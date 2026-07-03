import cv2
import os
import time

def collect_images(label, num_images=20, delay=1):
    """
    Skrip untuk mengambil gambar dari kamera untuk dataset baru.
    """
    dataset_dir = f"custom_dataset/{label}"
    os.makedirs(dataset_dir, exist_ok=True)
    
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("Error: Kamera tidak terdeteksi.")
        return

    print(f"Mulai mengambil {num_images} gambar untuk label: {label}")
    print("Siapkan objek di depan kamera...")
    time.sleep(3)

    count = 0
    while count < num_images:
        ret, frame = cap.read()
        if not ret:
            break
        
        img_name = os.path.join(dataset_dir, f"{label}_{int(time.time())}_{count}.jpg")
        cv2.imwrite(img_name, frame)
        print(f"Tersimpan: {img_name}")
        
        cv2.imshow("Data Collection", frame)
        count += 1
        time.sleep(delay)
        
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()
    print(f"Selesai! {count} gambar tersimpan di {dataset_dir}")

if __name__ == "__main__":
    # Contoh penggunaan: python collect_data.py
    label_input = input("Masukkan nama label (misal: botol_plastik): ")
    collect_images(label_input)
