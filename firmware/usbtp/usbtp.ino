// USB throughput probe. BAUD is set at compile time via -DTP_BAUD.
#ifndef TP_BAUD
#define TP_BAUD 921600
#endif
static uint8_t buf[1024];
void setup() {
  for (int i = 0; i < 1024; i++) buf[i] = (uint8_t)i;
  Serial.setTxBufferSize(8192);
  Serial.begin(TP_BAUD);
  delay(800);
}
void loop() { Serial.write(buf, sizeof(buf)); }
