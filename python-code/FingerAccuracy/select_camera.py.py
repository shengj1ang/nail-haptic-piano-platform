import cv2
import time

MAX_CAMERAS = 10


def test_camera(index):
    cap = cv2.VideoCapture(index)

    if not cap.isOpened():
        return False

    ret, frame = cap.read()
    cap.release()

    return ret and frame is not None


def preview_camera(index):
    cap = cv2.VideoCapture(index)

    if not cap.isOpened():
        print(f"Camera {index} cannot be opened.")
        return False

    print(f"\nPreviewing camera {index}")
    print("Press ENTER to select this camera.")
    print("Press N to try next camera.")
    print("Press Q to quit.")

    while True:
        ret, frame = cap.read()

        if not ret:
            print(f"Failed to read from camera {index}")
            break

        cv2.putText(
            frame,
            f"Camera index: {index}",
            (30, 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            1,
            (0, 255, 0),
            2,
        )

        cv2.imshow("Camera Preview", frame)

        key = cv2.waitKey(1) & 0xFF

        if key == 13:  # Enter
            cap.release()
            cv2.destroyAllWindows()
            return True

        if key == ord("n"):
            cap.release()
            cv2.destroyAllWindows()
            return False

        if key == ord("q"):
            cap.release()
            cv2.destroyAllWindows()
            exit()

    cap.release()
    cv2.destroyAllWindows()
    return False


def main():
    available_cameras = []

    print("Scanning cameras...")

    for i in range(MAX_CAMERAS):
        if test_camera(i):
            available_cameras.append(i)
            print(f"Camera {i}: available")
        else:
            print(f"Camera {i}: not available")

    if not available_cameras:
        print("No cameras found.")
        return

    print("\nAvailable cameras:", available_cameras)

    selected_camera = None

    for cam_index in available_cameras:
        selected = preview_camera(cam_index)
        if selected:
            selected_camera = cam_index
            break

    if selected_camera is None:
        print("No camera selected.")
    else:
        print(f"\nSelected camera index: {selected_camera}")


if __name__ == "__main__":
    main()