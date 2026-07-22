void setup() {
  pinMode(LED_BUILTIN, OUTPUT);  // 设置内置 LED
}

void loop() {
  digitalWrite(LED_BUILTIN, HIGH);  // 点亮
  delay(500);
  digitalWrite(LED_BUILTIN, LOW);   // 熄灭
  delay(500);
}