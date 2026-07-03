import os
from ultralytics import YOLO

def train_model(data_yaml_path, epochs=50, model_size='n'):
    """
    Melatih ulang model YOLOv8 dengan dataset baru.
    """
    model_name = f"yolov8{model_size}.pt"
    if not os.path.exists(model_name):
        print(f"Mengunduh model {model_name}...")
    
    model = YOLO(model_name)
    
    print(f"Memulai pelatihan selama {epochs} epoch menggunakan {data_yaml_path}...")
    results = model.train(data=data_yaml_path, epochs=epochs, imgsz=640)
    
    print("Pelatihan selesai!")
    print(f"Model terbaik tersimpan di: {results.save_dir}/weights/best.pt")
    return os.path.join(results.save_dir, 'weights', 'best.pt')

if __name__ == "__main__":
    # Path ke file YAML yang mendefinisikan dataset (harus dibuat pengguna sesuai standar YOLO)
    # Contoh isi data.yaml:
    # train: ../train/images
    # val: ../valid/images
    # nc: 1
    # names: ['botol_plastik']
    
    yaml_path = input("Masukkan path ke file data.yaml Anda: ")
    if os.path.exists(yaml_path):
        train_model(yaml_path)
    else:
        print("Error: File YAML tidak ditemukan.")
