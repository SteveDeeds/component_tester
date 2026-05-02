/*
 * POC Test Equipment Controller — SCPI Serial Interface
 *
 * HARDWARE MAPPING:
 * [OUTPUTS]
 * - V1 (Fast/High-Res): Pin D9 (PB1) | 12-bit PWM (0-4095) | Freq: 3.906 kHz
 *   Filter option A: R=1.3k, C=1.0uF  (tau 1.3ms,  settling ~6.5ms for 1mV ripple)
 *   Filter option B: R=100,  C=13uF   (tau 1.3ms,  settling ~6.5ms for 1mV ripple)
 *   Filter option C: R=82,  C=15uF    (tau 1.2ms,  settling ~6.2ms for 1mV ripple)
 * - V2 (Standard):      Pin D3 (PD3) | 8-bit  PWM (0-255)  | Freq: 490 Hz
 *   Filter option A: R=10k,  C=1.0uF  (tau 10ms,   settling ~50ms  for 1mV ripple)
 *   Filter option B: R=100,  C=100uF  (tau 10ms,   settling ~50ms  for 1mV ripple)
 *
 * [INPUTS]
 * - A0: V1 Voltage Sense (DUT side) A1: V1 Current Sense (TE side)
 * - A2: V2 Voltage Sense (DUT side) A3: V2 Current Sense (TE side)
 * - A4: DUT Ground ref / Ground Current (DUT SIDE, TE assumes zero volts)
 * - A5: DUT Supply Sense (Vs) / Supply Current Sense (DUT side, TE assumes 5.0volts)
 *
 * SCPI COMMAND SET (115200 8N1, LF or CR+LF terminated):
 * -------------------------------------------------------
 * IEEE 488.2 common:
 *   *IDN?               Returns: "<make>,<model>,<SN>,<FW>"
 *   *RST                Sets V1 and V2 to 0 V (no response)
 *
 * Source subsystem (voltages 0.0–5.0 V):
 *   SOUR1:VOLT <NRf>    Set V1 output voltage
 *   SOUR1:VOLT?         Returns V1 setpoint in V
 *   SOUR1:VOLT:MIN?     Returns V1 minimum voltage in V
 *   SOUR1:VOLT:MAX?     Returns V1 maximum voltage in V
 *   SOUR2:VOLT <NRf>    Set V2 output voltage
 *   SOUR2:VOLT?         Returns V2 setpoint in V
 *   SOUR2:VOLT:MIN?     Returns V2 minimum voltage in V
 *   SOUR2:VOLT:MAX?     Returns V2 maximum voltage in V
 *
 * Calibration subsystem:
 *   CAL:SHUN:V1 <NRf>    Set V1 shunt resistance in ohms
 *   CAL:SHUN:V1?         Returns V1 shunt resistance in ohms
 *   CAL:SHUN:V2 <NRf>    Set V2 shunt resistance in ohms
 *   CAL:SHUN:V2?         Returns V2 shunt resistance in ohms
 *   CAL:SHUN:GND <NRf>   Set GND shunt resistance in ohms
 *   CAL:SHUN:GND?        Returns GND shunt resistance in ohms
 *   CAL:SHUN:VS <NRf>    Set VS shunt resistance in ohms
 *   CAL:SHUN:VS?         Returns VS shunt resistance in ohms
 *
 * Sense subsystem:
 *   SENS:ADC:PRES <NR1>   Set ADC prescaler (2,4,8,16,32,64,128)
 *   SENS:ADC:PRES?        Returns ADC prescaler
 *   SENS:AVER:COUN <NR1>  Set number of averages per measurement (1–255)
 *   SENS:AVER:COUN?       Returns number of averages
 *
 * Waveform subsystem (capture-first then stream in engineering units):
 *   WAV:SIGN <LIST>       Set signal list (V1V,V1I,V2V,V2I,GNDI,VSV,VSI)
 *   WAV:SIGN?             Returns signal list
 *   WAV:POIN <NR1>        Set points per waveform
 *   WAV:POIN MAX          Set points to maximum for current mask
 *   WAV:POIN?             Returns points per waveform
 *   WAV:POIN:MAX?         Returns max points for current signal list
 *   WAV:DATA?             Capture to RAM then stream CSV: INDEX,<signal columns>
 *
 * Measure subsystem (all values returned in SI units: V or A):
 *   MEAS:VOLT1?         V1 voltage vs DUT ground
 *   MEAS:VOLT2?         V2 voltage vs DUT ground
 *   MEAS:CURR1?         V1 current out of source  [(A1-A0)/R_V1]
 *   MEAS:CURR2?         V2 current out of source  [(A3-A2)/R_V2]
 *   MEAS:CURR:GND?      Ground current out of ground [-A4/R_GND]
 *   MEAS:VS?            DUT supply voltage Vs vs DUT ground (A5-A4)
 *   MEAS:ALL?           CSV: V1V,V1I,V2V,V2I,GNDI,VSV,VSI
 *   MEAS:WAV?           Alias of WAV:DATA?
 *
 * System subsystem:
 *   SYST:ERR?             Returns and clears oldest queued error: <code>,"<desc>"
 *                         Returns 0,"No error" when queue is empty
 *
 * Error response (unknown/malformed command):
 *   ERROR,"<description>"
 */

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const float    VREF           = 5.0;
const int      ADC_MAX        = 1023;
const uint16_t PWM_12BIT_MAX  = 4095;
const uint8_t  PWM_8BIT_MAX   = 255;
const float    V1_SHUNT_OHMS_DEFAULT  = 1000.0; //99.4;
const float    V2_SHUNT_OHMS_DEFAULT  = 99.5;
const float    GND_SHUNT_OHMS_DEFAULT = 99.4;
const float    VS_SHUNT_OHMS_DEFAULT  = 99.0;
const uint8_t  ADC_PRESCALER_DEFAULT = 128;
// SRAM-critical on Uno: this drives g_wave_buffer[] size directly.
// 192 samples -> 384 B buffer (was 512 B at 256 samples).
const uint16_t WAVE_MAX_TOTAL_SAMPLES = 64;
const uint16_t WAVE_DEFAULT_POINTS = 64;
const uint16_t WAVE_SIGNAL_DEFAULT_MASK = 0x0001; // V1V
const float    SOUR1_VOLT_MIN = 0.0;   // V1 minimum voltage (V)
const float    SOUR1_VOLT_MAX = 5.0;   // V1 maximum voltage (V)
const float    SOUR2_VOLT_MIN = 0.0;   // V2 minimum voltage (V)
const float    SOUR2_VOLT_MAX = 5.0;   // V2 maximum voltage (V)

// ---------------------------------------------------------------------------
// SCPI error queue (depth 4, FIFO)
// ---------------------------------------------------------------------------
const uint8_t ERR_QUEUE_DEPTH = 2;
struct ScpiError {
  int16_t     code;
  const __FlashStringHelper *msg;
};
ScpiError g_err_queue[ERR_QUEUE_DEPTH];
uint8_t g_err_head = 0;  // index of next slot to read
uint8_t g_err_tail = 0;  // index of next slot to write
uint8_t g_err_count = 0;

// Retained setpoints (for SOUR:VOLT? queries)
float g_v1_setpoint = 0.0;
float g_v2_setpoint = 0.0;
float g_v1_shunt_ohms = V1_SHUNT_OHMS_DEFAULT;
float g_v2_shunt_ohms = V2_SHUNT_OHMS_DEFAULT;
float g_gnd_shunt_ohms = GND_SHUNT_OHMS_DEFAULT;
float g_vs_shunt_ohms = VS_SHUNT_OHMS_DEFAULT;
uint8_t g_adc_prescaler = ADC_PRESCALER_DEFAULT;
uint8_t g_avg_count = 1;
uint16_t g_wave_signal_mask = WAVE_SIGNAL_DEFAULT_MASK;
uint16_t g_wave_points = WAVE_DEFAULT_POINTS;
uint16_t g_wave_buffer[WAVE_MAX_TOTAL_SAMPLES];

enum WaveSignalBit {
  SIG_V1V = 0,
  SIG_V1I,
  SIG_V2V,
  SIG_V2I,
  SIG_GNDI,
  SIG_VSV,
  SIG_VSI,
  SIG_COUNT
};

// ---------------------------------------------------------------------------
// Hardware helpers
// ---------------------------------------------------------------------------
float adcToVolts(int raw) {
  return (raw / (float)ADC_MAX) * VREF;
}

uint8_t countEnabledWaveChannels(uint8_t mask) {
  uint8_t n = 0;
  for (uint8_t ch = 0; ch < 8; ch++) {
    if (mask & (1 << ch)) n++;
  }
  return n;
}

uint8_t countEnabledWaveSignals(uint16_t mask) {
  uint8_t n = 0;
  for (uint8_t bit = 0; bit < SIG_COUNT; bit++) {
    if (mask & (1 << bit)) n++;
  }
  return n;
}

uint16_t adcMaskForSignalMask(uint16_t signalMask) {
  uint16_t mask = 0;

  if (signalMask & (1 << SIG_V1V))  mask |= (1 << 0) | (1 << 4); // A0, A4
  if (signalMask & (1 << SIG_V1I))  mask |= (1 << 0) | (1 << 1); // A0, A1
  if (signalMask & (1 << SIG_V2V))  mask |= (1 << 2) | (1 << 4); // A2, A4
  if (signalMask & (1 << SIG_V2I))  mask |= (1 << 2) | (1 << 3); // A2, A3
  if (signalMask & (1 << SIG_GNDI)) mask |= (1 << 4);            // A4
  if (signalMask & (1 << SIG_VSV))  mask |= (1 << 5);            // A5
  if (signalMask & (1 << SIG_VSI))  mask |= (1 << 5);            // A5

  return mask;
}

uint16_t maxWavePointsForAdcMask(uint8_t adcMask) {
  uint8_t channels = countEnabledWaveChannels(adcMask);
  if (channels == 0) return 0;
  return WAVE_MAX_TOTAL_SAMPLES / channels;
}

const char* waveSignalName(uint8_t bit) {
  switch (bit) {
    case SIG_V1V: return "V1_V";
    case SIG_V1I: return "V1_I_A";
    case SIG_V2V: return "V2_V";
    case SIG_V2I: return "V2_I_A";
    case SIG_GNDI: return "GND_I_A";
    case SIG_VSV: return "VS_V";
    case SIG_VSI: return "VS_I_A";
    default: return "UNK";
  }
}

bool parseWaveSignalToken(const String &token, uint8_t &bitOut) {
  String t = token;
  t.trim();
  t.toUpperCase();

  if (t == "V1V") { bitOut = SIG_V1V; return true; }
  if (t == "V1I") { bitOut = SIG_V1I; return true; }
  if (t == "V2V") { bitOut = SIG_V2V; return true; }
  if (t == "V2I") { bitOut = SIG_V2I; return true; }
  if (t == "GNDI") { bitOut = SIG_GNDI; return true; }
  if (t == "VSV") { bitOut = SIG_VSV; return true; }
  if (t == "VSI") { bitOut = SIG_VSI; return true; }
  if (t == "AUX1V") { bitOut = SIG_VSV; return true; } // Backward-compatible alias for VSV

  return false;
}

bool parseWaveSignalList(const String &arg, uint16_t &maskOut) {
  String list = arg;
  list.trim();

  String listUpper = list;
  listUpper.toUpperCase();
  if (listUpper == "ALL") {
    maskOut = 0;
    for (uint8_t bit = 0; bit < SIG_COUNT; bit++) {
      maskOut |= (1 << bit);
    }
    return true;
  }

  uint16_t mask = 0;
  int start = 0;

  while (start <= list.length()) {
    int comma = list.indexOf(',', start);
    String token;

    if (comma == -1) {
      token = list.substring(start);
      start = list.length() + 1;
    } else {
      token = list.substring(start, comma);
      start = comma + 1;
    }

    token.trim();
    if (token.length() == 0) continue;

    uint8_t bit;
    if (!parseWaveSignalToken(token, bit)) {
      return false;
    }

    mask |= (1 << bit);
  }

  if (mask == 0) return false;

  maskOut = mask;
  return true;
}

uint8_t buildAdcChannelOrder(uint8_t adcMask, uint8_t order[8]) {
  uint8_t n = 0;
  for (uint8_t ch = 0; ch < 8; ch++) {
    if (adcMask & (1 << ch)) {
      order[n++] = ch;
    }
  }
  return n;
}

float signalValueFromRaw(uint8_t signalBit, const uint16_t rawByChannel[8]) {
  float a0 = adcToVolts(rawByChannel[0]);
  float a1 = adcToVolts(rawByChannel[1]);
  float a2 = adcToVolts(rawByChannel[2]);
  float a3 = adcToVolts(rawByChannel[3]);
  float a4 = adcToVolts(rawByChannel[4]);
  float a5 = adcToVolts(rawByChannel[5]);
  float a6 = adcToVolts(rawByChannel[6]);
  float a7 = adcToVolts(rawByChannel[7]);

  switch (signalBit) {
    case SIG_V1V: return a0 - a4;
    case SIG_V1I: return (a1 - a0) / g_v1_shunt_ohms;
    case SIG_V2V: return a2 - a4;
    case SIG_V2I: return (a3 - a2) / g_v2_shunt_ohms;
    case SIG_GNDI: return a4 / g_gnd_shunt_ohms;
    case SIG_VSV: return a5 - a4;
    case SIG_VSI: return (VREF - a5) / g_vs_shunt_ohms;
    default: return 0.0;
  }
}

void printWaveSignalList(uint16_t signalMask) {
  bool first = true;
  for (uint8_t bit = 0; bit < SIG_COUNT; bit++) {
    if (signalMask & (1 << bit)) {
      if (!first) Serial.print(',');
      Serial.print(waveSignalName(bit));
      first = false;
    }
  }
  Serial.println();
}

uint16_t maxWavePointsForSignalMask(uint16_t signalMask) {
  uint8_t adcMask = (uint8_t)adcMaskForSignalMask(signalMask);
  return maxWavePointsForAdcMask(adcMask);
}

uint16_t readAdcRawFast(uint8_t channel) {
  // AVcc reference, right-adjusted result, channel 0..7.
  ADMUX = _BV(REFS0) | (channel & 0x07);
  ADCSRA |= _BV(ADSC);
  while (ADCSRA & _BV(ADSC)) {}
  return ADC;
}

bool setAdcPrescaler(uint8_t prescaler) {
  uint8_t bits;

  switch (prescaler) {
    case 2:   bits = _BV(ADPS0); break;
    case 4:   bits = _BV(ADPS1); break;
    case 8:   bits = _BV(ADPS1) | _BV(ADPS0); break;
    case 16:  bits = _BV(ADPS2); break;
    case 32:  bits = _BV(ADPS2) | _BV(ADPS0); break;
    case 64:  bits = _BV(ADPS2) | _BV(ADPS1); break;
    case 128: bits = _BV(ADPS2) | _BV(ADPS1) | _BV(ADPS0); break;
    default: return false;
  }

  ADCSRA = (ADCSRA & ~(_BV(ADPS2) | _BV(ADPS1) | _BV(ADPS0))) | bits;
  g_adc_prescaler = prescaler;
  return true;
}

float readAnalogVolts(uint8_t pin) {
  // Throw away first conversion after mux switch for improved channel-to-channel repeatability.
  analogRead(pin);
  return adcToVolts(analogRead(pin));
}

bool captureWaveformToBuffer(uint8_t adcMask, uint16_t pointsPerWave, uint8_t &enabledChannels, uint8_t channelOrder[8]) {
  enabledChannels = buildAdcChannelOrder(adcMask, channelOrder);
  if (enabledChannels == 0) return false;

  uint32_t totalSamples = (uint32_t)enabledChannels * (uint32_t)pointsPerWave;
  if (totalSamples > WAVE_MAX_TOTAL_SAMPLES) return false;

  uint16_t idx = 0;
  for (uint16_t i = 0; i < pointsPerWave; i++) {
    for (uint8_t c = 0; c < enabledChannels; c++) {
      g_wave_buffer[idx++] = readAdcRawFast(channelOrder[c]);
    }
  }

  return true;
}

void streamWaveformCsv(uint16_t signalMask, uint16_t pointsPerWave, uint8_t enabledChannels) {
  Serial.print("WAV,POINTS,");
  Serial.print(pointsPerWave);
  Serial.print(",RAW_CHANNELS,");
  Serial.print(enabledChannels);
  Serial.print(",SIGNALS,");
  Serial.println(countEnabledWaveSignals(signalMask));

  Serial.print("INDEX");
  for (uint8_t bit = 0; bit < SIG_COUNT; bit++) {
    if (signalMask & (1 << bit)) {
      Serial.print(',');
      Serial.print(waveSignalName(bit));
    }
  }
  Serial.println();

  uint16_t idx = 0;
  for (uint16_t i = 0; i < pointsPerWave; i++) {
    uint16_t rawByChannel[8] = {0, 0, 0, 0, 0, 0, 0, 0};

    uint8_t adcMask = (uint8_t)adcMaskForSignalMask(signalMask);
    for (uint8_t ch = 0; ch < 8; ch++) {
      if (adcMask & (1 << ch)) {
        rawByChannel[ch] = g_wave_buffer[idx++];
      }
    }

    Serial.print(i);
    for (uint8_t bit = 0; bit < SIG_COUNT; bit++) {
      if (signalMask & (1 << bit)) {
        Serial.print(',');
        Serial.print(signalValueFromRaw(bit, rawByChannel), 6);
      }
    }
    Serial.println();
  }

  Serial.println("WAV,END");
}

void setV1(float voltage) {
  g_v1_setpoint = constrain(voltage, 0.0, VREF);
  OCR1A = (uint16_t)((g_v1_setpoint / VREF) * PWM_12BIT_MAX);
}

void setV2(float voltage) {
  g_v2_setpoint = constrain(voltage, 0.0, VREF);
  analogWrite(3, (uint8_t)((g_v2_setpoint / VREF) * PWM_8BIT_MAX));
}

void initHardwareDefaults() {
  // Timer 1 — 12-bit Fast PWM on D9 (OC1A)
  // Mode 14: ICR1 as TOP | No prescaler -> 16 MHz / 4096 ~= 3.906 kHz
  pinMode(9, OUTPUT);
  TCCR1A = _BV(COM1A1) | _BV(WGM11);
  TCCR1B = _BV(WGM13) | _BV(WGM12) | _BV(CS10);
  ICR1 = PWM_12BIT_MAX;

  // Timer 2 — standard 8-bit PWM on D3 (490 Hz default)
  pinMode(3, OUTPUT);

  setAdcPrescaler(ADC_PRESCALER_DEFAULT);
}

void resetInstrumentState() {
  initHardwareDefaults();
  setV1(0.0);
  setV2(0.0);
  g_v1_shunt_ohms = V1_SHUNT_OHMS_DEFAULT;
  g_v2_shunt_ohms = V2_SHUNT_OHMS_DEFAULT;
  g_gnd_shunt_ohms = GND_SHUNT_OHMS_DEFAULT;
  g_vs_shunt_ohms = VS_SHUNT_OHMS_DEFAULT;
  g_wave_signal_mask = WAVE_SIGNAL_DEFAULT_MASK;
  uint16_t maxPoints = maxWavePointsForSignalMask(g_wave_signal_mask);
  g_wave_points = (WAVE_DEFAULT_POINTS <= maxPoints) ? WAVE_DEFAULT_POINTS : maxPoints;
}

// ---------------------------------------------------------------------------
// Forward declarations
// ---------------------------------------------------------------------------
void scpiError(const __FlashStringHelper *msg, bool isQuery = false);
void errEnqueue(int16_t code, const __FlashStringHelper *msg);
void errClearQueue();

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);

  resetInstrumentState();
}

// ---------------------------------------------------------------------------
// Main loop — read full SCPI line
// ---------------------------------------------------------------------------
void loop() {
  if (Serial.available() > 0) {
    String line = Serial.readStringUntil('\n');
    line.trim();
    if (line.length() > 0) {
      processScpi(line);
    }
  }
}

// ---------------------------------------------------------------------------
// SCPI dispatcher
// ---------------------------------------------------------------------------
void processScpi(const String &raw) {
  String cmd = raw;
  cmd.trim();

  // Work on an uppercase copy for case-insensitive matching
  String upper = cmd;
  upper.toUpperCase();

  bool isQuery = upper.endsWith("?");

  // ---- IEEE 488.2 common commands ----------------------------------------
  if (upper == "*IDN?") {
    Serial.println(F("Arduino,ComponentTester,SN001,FW1.0"));
    return;
  }

  if (upper == "*RST") {
    resetInstrumentState();
    errClearQueue();
    return; // no response for non-query commands
  }

  // ---- SYST:ERR? --------------------------------------------------------
  if (upper == "SYST:ERR?") {
    if (g_err_count == 0) {
      Serial.println(F("0,\"No error\""));
    } else {
      ScpiError &e = g_err_queue[g_err_head];
      Serial.print(e.code);
      Serial.print(",\"");
      Serial.print(e.msg);
      Serial.println("\"");
      g_err_head = (g_err_head + 1) % ERR_QUEUE_DEPTH;
      g_err_count--;
    }
    return;
  }

  // ---- SOUR1:VOLT --------------------------------------------------------
  if (upper == "SOUR1:VOLT?" || upper.startsWith("SOUR1:VOLT ")) {
    if (isQuery) {
      Serial.println(g_v1_setpoint, 6);
    } else {
      int sp = cmd.indexOf(' ');
      if (sp == -1) { scpiError(F("Missing value for SOUR1:VOLT"), isQuery); return; }
      setV1(cmd.substring(sp + 1).toFloat());
    }
    return;
  }

  // ---- SOUR2:VOLT --------------------------------------------------------
  if (upper == "SOUR2:VOLT?" || upper.startsWith("SOUR2:VOLT ")) {
    if (isQuery) {
      Serial.println(g_v2_setpoint, 6);
    } else {
      int sp = cmd.indexOf(' ');
      if (sp == -1) { scpiError(F("Missing value for SOUR2:VOLT"), isQuery); return; }
      setV2(cmd.substring(sp + 1).toFloat());
    }
    return;
  }

  // ---- SOUR1:VOLT:MIN ---------------------------------------------------
  if (upper.startsWith("SOUR1:VOLT:MIN")) {
    if (isQuery) {
      Serial.println(SOUR1_VOLT_MIN, 6);
    } else {
      scpiError(F("SOUR1:VOLT:MIN is read-only"), isQuery);
    }
    return;
  }

  // ---- SOUR1:VOLT:MAX ---------------------------------------------------
  if (upper.startsWith("SOUR1:VOLT:MAX")) {
    if (isQuery) {
      Serial.println(SOUR1_VOLT_MAX, 6);
    } else {
      scpiError(F("SOUR1:VOLT:MAX is read-only"), isQuery);
    }
    return;
  }

  // ---- SOUR2:VOLT:MIN ---------------------------------------------------
  if (upper.startsWith("SOUR2:VOLT:MIN")) {
    if (isQuery) {
      Serial.println(SOUR2_VOLT_MIN, 6);
    } else {
      scpiError(F("SOUR2:VOLT:MIN is read-only"), isQuery);
    }
    return;
  }

  // ---- SOUR2:VOLT:MAX ---------------------------------------------------
  if (upper.startsWith("SOUR2:VOLT:MAX")) {
    if (isQuery) {
      Serial.println(SOUR2_VOLT_MAX, 6);
    } else {
      scpiError(F("SOUR2:VOLT:MAX is read-only"), isQuery);
    }
    return;
  }

  // ---- CAL:SHUN:V1 ------------------------------------------------------
  if (upper == "CAL:SHUN:V1?" || upper.startsWith("CAL:SHUN:V1 ")) {
    if (isQuery) {
      Serial.println(g_v1_shunt_ohms, 6);
    } else {
      int sp = cmd.indexOf(' ');
      if (sp == -1) { scpiError(F("Missing value for CAL:SHUN:V1"), isQuery); return; }
      float value = cmd.substring(sp + 1).toFloat();
      if (!(value > 0.0)) { scpiError(F("CAL:SHUN:V1 must be > 0"), isQuery); return; }
      g_v1_shunt_ohms = value;
    }
    return;
  }

  // ---- CAL:SHUN:V2 ------------------------------------------------------
  if (upper == "CAL:SHUN:V2?" || upper.startsWith("CAL:SHUN:V2 ")) {
    if (isQuery) {
      Serial.println(g_v2_shunt_ohms, 6);
    } else {
      int sp = cmd.indexOf(' ');
      if (sp == -1) { scpiError(F("Missing value for CAL:SHUN:V2"), isQuery); return; }
      float value = cmd.substring(sp + 1).toFloat();
      if (!(value > 0.0)) { scpiError(F("CAL:SHUN:V2 must be > 0"), isQuery); return; }
      g_v2_shunt_ohms = value;
    }
    return;
  }

  // ---- CAL:SHUN:GND -----------------------------------------------------
  if (upper == "CAL:SHUN:GND?" || upper.startsWith("CAL:SHUN:GND ")) {
    if (isQuery) {
      Serial.println(g_gnd_shunt_ohms, 6);
    } else {
      int sp = cmd.indexOf(' ');
      if (sp == -1) { scpiError(F("Missing value for CAL:SHUN:GND"), isQuery); return; }
      float value = cmd.substring(sp + 1).toFloat();
      if (!(value > 0.0)) { scpiError(F("CAL:SHUN:GND must be > 0"), isQuery); return; }
      g_gnd_shunt_ohms = value;
    }
    return;
  }

  // ---- CAL:SHUN:VS ------------------------------------------------------
  if (upper == "CAL:SHUN:VS?" || upper.startsWith("CAL:SHUN:VS ")) {
    if (isQuery) {
      Serial.println(g_vs_shunt_ohms, 6);
    } else {
      int sp = cmd.indexOf(' ');
      if (sp == -1) { scpiError(F("Missing value for CAL:SHUN:VS"), isQuery); return; }
      float value = cmd.substring(sp + 1).toFloat();
      if (!(value > 0.0)) { scpiError(F("CAL:SHUN:VS must be > 0"), isQuery); return; }
      g_vs_shunt_ohms = value;
    }
    return;
  }

  // ---- SENS:AVER:COUN --------------------------------------------------
  if (upper.startsWith("SENS:AVER:COUN")) {
    if (isQuery) {
      Serial.println(g_avg_count);
    } else {
      int sp = cmd.indexOf(' ');
      if (sp == -1) { scpiError(F("Missing value for SENS:AVER:COUN"), isQuery); return; }
      long requested = cmd.substring(sp + 1).toInt();
      if (requested < 1 || requested > 255) {
        scpiError(F("SENS:AVER:COUN out of range (1-255)"), isQuery);
        return;
      }
      g_avg_count = (uint8_t)requested;
    }
    return;
  }

  // ---- SENS:ADC:PRES ----------------------------------------------------
  if (upper.startsWith("SENS:ADC:PRES")) {
    if (isQuery) {
      Serial.println(g_adc_prescaler);
    } else {
      int sp = cmd.indexOf(' ');
      if (sp == -1) { scpiError(F("Missing value for SENS:ADC:PRES"), isQuery); return; }
      long requested = cmd.substring(sp + 1).toInt();
      if (!setAdcPrescaler((uint8_t)requested)) {
        scpiError(F("SENS:ADC:PRES invalid (2,4,8,16,32,64,128)"), isQuery);
        return;
      }
    }
    return;
  }

  // ---- WAV:SIGN ---------------------------------------------------------
  if (upper.startsWith("WAV:SIGN")) {
    if (isQuery) {
      printWaveSignalList(g_wave_signal_mask);
    } else {
      int sp = cmd.indexOf(' ');
      if (sp == -1) { scpiError(F("Missing value for WAV:SIGN"), isQuery); return; }

      uint16_t newMask;
      if (!parseWaveSignalList(cmd.substring(sp + 1), newMask)) {
        scpiError(F("WAV:SIGN invalid list"), isQuery);
        return;
      }

      g_wave_signal_mask = newMask;

      uint16_t maxPoints = maxWavePointsForSignalMask(g_wave_signal_mask);
      if (g_wave_points > maxPoints) {
        g_wave_points = maxPoints;
      }
    }
    return;
  }

  // ---- WAV:POIN ---------------------------------------------------------
  if (upper.startsWith("WAV:POIN:MAX")) {
    if (!isQuery) { scpiError(F("WAV:POIN:MAX is query-only"), isQuery); return; }
    Serial.println(maxWavePointsForSignalMask(g_wave_signal_mask));
    return;
  }

  if (upper.startsWith("WAV:POIN")) {
    if (isQuery) {
      Serial.println(g_wave_points);
    } else {
      int sp = cmd.indexOf(' ');
      if (sp == -1) { scpiError(F("Missing value for WAV:POIN"), isQuery); return; }
      String arg = cmd.substring(sp + 1);
      arg.trim();
      String argUpper = arg;
      argUpper.toUpperCase();
      uint16_t maxPoints = maxWavePointsForSignalMask(g_wave_signal_mask);

      if (argUpper == "MAX") {
        g_wave_points = maxPoints;
        return;
      }

      long requested = arg.toInt();
      if (requested < 1 || requested > maxPoints) {
        scpiError(F("WAV:POIN out of range for current signal list"), isQuery);
        return;
      }
      g_wave_points = (uint16_t)requested;
    }
    return;
  }

  // ---- Waveform capture + transfer --------------------------------------
  // MEAS:WAV? is an alias of WAV:DATA?.
  if (upper == "WAV:DATA?" || upper == "MEAS:WAV?") {
    uint8_t enabledChannels = 0;
    uint8_t channelOrder[8];
    uint8_t adcMask = (uint8_t)adcMaskForSignalMask(g_wave_signal_mask);

    if (!captureWaveformToBuffer(adcMask, g_wave_points, enabledChannels, channelOrder)) {
      scpiError(F("Waveform capture configuration invalid"), isQuery);
      return;
    }
    streamWaveformCsv(g_wave_signal_mask, g_wave_points, enabledChannels);
    return;
  }

  // ---- MEAS queries (all require '?') ------------------------------------
  if (!isQuery) {
    scpiError(F("Unrecognised command"), isQuery);
    return;
  }

  if (upper == "MEAS:VOLT1?") {
    float a0 = readAnalogVolts(A0);
    float a4 = readAnalogVolts(A4);
    Serial.println(a0 - a4, 6);
    return;
  }

  if (upper == "MEAS:VOLT2?") {
    float a2 = readAnalogVolts(A2);
    float a4 = readAnalogVolts(A4);
    Serial.println(a2 - a4, 6);
    return;
  }

  if (upper == "MEAS:CURR1?") {
    float a0 = readAnalogVolts(A0);
    float a1 = readAnalogVolts(A1);
    Serial.println((a1 - a0) / g_v1_shunt_ohms, 6);
    return;
  }

  if (upper == "MEAS:CURR2?") {
    float a2 = readAnalogVolts(A2);
    float a3 = readAnalogVolts(A3);
    Serial.println((a3 - a2) / g_v2_shunt_ohms, 6);
    return;
  }

  if (upper == "MEAS:CURR:GND?") {
    Serial.println(-readAnalogVolts(A4) / g_gnd_shunt_ohms, 6);
    return;
  }

  if (upper == "MEAS:VS?" || upper == "MEAS:AUX1?") {
    float a5 = readAnalogVolts(A5);
    float a4 = readAnalogVolts(A4);
    Serial.println(a5 - a4, 6);
    return;
  }

  if (upper == "MEAS:ALL?") {
    measAll();
    return;
  }

  scpiError(F("Unrecognised command"), isQuery);
}

// ---------------------------------------------------------------------------
// MEAS:ALL? — CSV: V1V,V1I,V2V,V2I,GNDI,VSV,VSI  (SI: V and A)
// ---------------------------------------------------------------------------
void measAll() {
  float avg[7] = {0, 0, 0, 0, 0, 0, 0};

  for (uint16_t i = 1; i <= g_avg_count; i++) {
    float a0 = readAnalogVolts(A0);
    float a1 = readAnalogVolts(A1);
    float a2 = readAnalogVolts(A2);
    float a3 = readAnalogVolts(A3);
    float a4 = readAnalogVolts(A4);
    float a5 = readAnalogVolts(A5);

    float sample[7] = {
      a0 - a4,
      (a1 - a0) / g_v1_shunt_ohms,
      a2 - a4,
      (a3 - a2) / g_v2_shunt_ohms,
      a4 / g_gnd_shunt_ohms,
      a5 - a4,
      (VREF - a5) / g_vs_shunt_ohms
    };

    // Incremental mean: avg = avg + (sample - avg) / i
    for (uint8_t ch = 0; ch < 7; ch++) {
      avg[ch] += (sample[ch] - avg[ch]) / (float)i;
    }
  }

  for (int i = 0; i < 7; i++) {
    Serial.print(avg[i], 6);
    if (i < 6) Serial.print(',');
  }
  Serial.println();
}

// ---------------------------------------------------------------------------
// Error queue helpers
// ---------------------------------------------------------------------------
void errEnqueue(int16_t code, const __FlashStringHelper *msg) {
  if (g_err_count >= ERR_QUEUE_DEPTH) return; // queue full, oldest error already there
  g_err_queue[g_err_tail].code = code;
  g_err_queue[g_err_tail].msg  = msg;
  g_err_tail = (g_err_tail + 1) % ERR_QUEUE_DEPTH;
  g_err_count++;
}

void errClearQueue() {
  g_err_head = g_err_tail = g_err_count = 0;
}

// ---------------------------------------------------------------------------
// Error response — always enqueues; also responds inline for queries
// (SCPI: set commands are silent, queries may return ERROR,"...")
// ---------------------------------------------------------------------------
void scpiError(const __FlashStringHelper *msg, bool isQuery) {
  errEnqueue(-100, msg);
  if (!isQuery) return;
  Serial.print(F("ERROR,\""));
  Serial.print(msg);
  Serial.println(F("\""));
}