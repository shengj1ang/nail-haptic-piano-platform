int pwm1 = 2;
int dir1 = 3;
int pwm2 = 4;
int pwm3 = 6;

// ===== 可调参数 =====
int power = 100;        // 震动强度 (0~255)
int vibrateTime = 80;   // 每次震动时间 (ms)
int gapTime = 300;      // 震动之间的间隔 (ms)

void setup() {
  pinMode(pwm1, OUTPUT);
  pinMode(pwm2, OUTPUT);
  pinMode(pwm3, OUTPUT);
  pinMode(dir1, OUTPUT);

  digitalWrite(dir1, HIGH);

  analogWriteFrequency(pwm1, 240);  // 如果你的板子支持
}

void vibrateOnce(int pin) {
  analogWrite(pin, power);
  delay(vibrateTime);
  analogWrite(pin, 0);
  delay(gapTime);
}

void loop() {
  vibrateOnce(pwm1);
  vibrateOnce(pwm2);
  vibrateOnce(pwm3);
}