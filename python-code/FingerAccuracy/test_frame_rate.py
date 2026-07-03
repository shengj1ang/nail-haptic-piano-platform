import cv2
import time

cap = cv2.VideoCapture(0)

cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)

cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

cap.set(cv2.CAP_PROP_FPS, 120)


# 尝试设置高帧率
cap.set(cv2.CAP_PROP_FPS, 120)

print("Requested FPS:", 120)
print("Camera reports FPS:", cap.get(cv2.CAP_PROP_FPS))

frame_count = 0
start_time = time.time()

while True:
    ret, frame = cap.read()

    if not ret:
        break

    frame_count += 1

    elapsed = time.time() - start_time

    if elapsed >= 5:
        actual_fps = frame_count / elapsed
        print(f"Actual FPS: {actual_fps:.2f}")
        frame_count = 0
        start_time = time.time()

    cv2.imshow("Camera Test", frame)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()