import cv2
import time
import json
import os
import threading
import queue
import platform
from ultralytics import YOLO
from openclaw_api import send_to_whatsapp

# Load Config
with open('config.json', 'r') as f:
    cfg = json.load(f)

DEBUG = cfg.get('debug', True)  # tampilkan kotak deteksi, skeleton pose, & angka mentah di terminal

# Load Models
obj_model = YOLO(cfg.get('yolo_model_path', 'yolov8m.pt'))
pose_model = YOLO('yolov8m-pose.pt')


LITTER_MODEL_PATH = cfg.get('litter_model_path', 'turhancan97/yolov8-segment-trash-detection')
litter_model = None
if cfg.get('enable_litter_model', True):
    try:
        litter_model = YOLO(LITTER_MODEL_PATH)
        print(f"[MODEL] Model sampah khusus (TACO) berhasil dimuat: {LITTER_MODEL_PATH}")
    except Exception as e:
        print(f"[MODEL] Gagal memuat model sampah khusus ({LITTER_MODEL_PATH}): {e}")
        print("[MODEL] Lanjut hanya dengan model COCO biasa (obj_model).")
        litter_model = None

last_sent = 0


TRASH_CLASSES_DEFINITE = [
    'bottle', 'wine glass', 'cup', 'bowl',                      
    'fork', 'knife', 'spoon',                                    
    'banana', 'apple', 'sandwich', 'orange', 'broccoli',
    'carrot', 'hot dog', 'pizza', 'donut', 'cake',               
]
TRASH_CLASSES_AMBIGUOUS = [
    'backpack', 'handbag', 'suitcase',    
    'umbrella',                           
    'book',                               
    'toothbrush',                         
    'vase',                               
]
AMBIGUOUS_CONF_BONUS = 0.15   


HAND_ABOVE_SHOULDER_MARGIN = 0.0   
ELBOW_ABOVE_HIP_MARGIN = 0.0     
HAND_BELOW_HIP_MARGIN = 0.05      
HAND_SIDEWAYS_MARGIN = 0.35        
FACE_EXCLUSION_RATIO = 0.55      
MIN_BODY_SCALE = 25               


TRASH_LINGER_SECONDS = cfg.get('trash_linger_seconds', 10.0)
TRASH_GAP_TOLERANCE = 1.0  


CAM_WIDTH = 1280        
CAM_HEIGHT = 720
INFER_IMGSZ = 640       


ENABLE_TILING = cfg.get('enable_tiling', True)
TILE_GRID = tuple(cfg.get('tile_grid', [2, 2]))
TILE_OVERLAP = 0.15
TILE_IOU_MERGE_THRESH = 0.4


def get_tiles(frame_width, frame_height, grid=TILE_GRID, overlap=TILE_OVERLAP):
    """Bagi frame jadi grid tile dengan sedikit overlap. Return list (x1,y1,x2,y2) koordinat ASLI frame."""
    rows, cols = grid
    tile_h = frame_height / rows
    tile_w = frame_width / cols
    pad_h = tile_h * overlap
    pad_w = tile_w * overlap

    tiles = []
    for r in range(rows):
        for c in range(cols):
            y1 = max(0, int(r * tile_h - pad_h))
            y2 = min(frame_height, int((r + 1) * tile_h + pad_h))
            x1 = max(0, int(c * tile_w - pad_w))
            x2 = min(frame_width, int((c + 1) * tile_w + pad_w))
            tiles.append((x1, y1, x2, y2))
    return tiles


def _box_iou(a, b):
    xa1, ya1 = max(a[0], b[0]), max(a[1], b[1])
    xa2, ya2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, xa2 - xa1) * max(0, ya2 - ya1)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0


def merge_tile_detections(detections):
    """
    detections: list of (box, conf, tier). Tile saling overlap -> 1 objek fisik
    bisa terdeteksi >1 kali. Gabungkan yang overlap (IoU tinggi), simpan confidence tertinggi.
    """
    if not detections:
        return []
    order = sorted(range(len(detections)), key=lambda i: detections[i][1], reverse=True)
    used = [False] * len(detections)
    kept = []
    for i in order:
        if used[i]:
            continue
        kept.append(detections[i])
        for j in order:
            if not used[j] and _box_iou(detections[i][0], detections[j][0]) > TILE_IOU_MERGE_THRESH:
                used[j] = True
    return kept


class VideoStream:
    """
    Membaca frame kamera di thread terpisah supaya loop utama (yang berisi
    inferensi YOLO yang berat) tidak memblokir/menumpuk antrian frame kamera.
    """
    def __init__(self, src=0, width=CAM_WIDTH, height=CAM_HEIGHT):
        self.cap = self._open_camera(src)

        if not self.cap.isOpened():
            raise RuntimeError(
                f"Tidak bisa membuka kamera index {src}. Kemungkinan penyebab:\n"
                f"  1. Kamera sedang dipakai aplikasi lain (Zoom, browser, dll) -- tutup dulu aplikasinya.\n"
                f"  2. Index kamera salah -- coba ganti src=0 jadi src=1 atau src=2 di kode.\n"
                f"  3. Izin kamera Windows dimatikan utk aplikasi desktop -- cek Settings > Privacy > Camera.\n"
                f"  4. Kamera USB tidak plug-in dengan benar -- coba cabut-colok ulang."
            )

        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        self.ret, self.frame = self.cap.read()
        if not self.ret or self.frame is None:
            raise RuntimeError(
                f"Kamera index {src} berhasil dibuka, tapi gagal membaca frame pertama "
                f"(sering karena izin kamera Windows utk aplikasi desktop dimatikan)."
            )

        actual_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        print(f"[KAMERA] Berhasil dibuka (index={src}), resolusi aktual: {actual_w}x{actual_h}")

        self.lock = threading.Lock()
        self.stopped = False
        self.thread = threading.Thread(target=self._update, daemon=True)
        self.thread.start()

    @staticmethod
    def _try_open_with_timeout(src, backend, timeout=4.0):
        """cv2.VideoCapture() kadang HANG total di beberapa sistem -- ini mencegah program diam selamanya."""
        result_q = queue.Queue()

        def worker():
            try:
                c = cv2.VideoCapture(src, backend) if backend is not None else cv2.VideoCapture(src)
                result_q.put(c)
            except Exception as e:
                result_q.put(e)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout)

        if t.is_alive():
            return None, True
        try:
            result = result_q.get_nowait()
        except queue.Empty:
            return None, False
        if isinstance(result, Exception) or result is None:
            return None, False
        return result, False

    @classmethod
    def _open_camera(cls, src):
        """Coba backend sesuai OS (default OpenCV kadang hang); tiap percobaan dibatasi waktu."""
        system = platform.system()
        if system == "Windows":
            candidates = [("DirectShow", cv2.CAP_DSHOW), ("Media Foundation", cv2.CAP_MSMF), ("Default", None)]
        elif system == "Linux":
            candidates = [("V4L2", cv2.CAP_V4L2), ("Default", None)]
        elif system == "Darwin":
            candidates = [("AVFoundation", cv2.CAP_AVFOUNDATION), ("Default", None)]
        else:
            candidates = [("Default", None)]

        for name, backend in candidates:
            print(f"[KAMERA] Mencoba backend {name}...")
            cap, timed_out = cls._try_open_with_timeout(src, backend, timeout=4.0)
            if timed_out:
                print(f"[KAMERA] Backend {name} menggantung >4 detik, dilewati.")
                continue
            if cap is not None and cap.isOpened():
                print(f"[KAMERA] Berhasil dibuka dengan backend {name}.")
                return cap
            if cap is not None:
                cap.release()
            print(f"[KAMERA] Backend {name} gagal membuka kamera.")

        return cv2.VideoCapture(src)

    def _update(self):
        while not self.stopped:
            ret, frame = self.cap.read()
            if not ret:
                self.stopped = True
                break
            with self.lock:
                self.ret, self.frame = ret, frame

    def read(self):
        with self.lock:
            return self.ret, (self.frame.copy() if self.frame is not None else None)

    def isOpened(self):
        return self.cap.isOpened() and not self.stopped

    def release(self):
        self.stopped = True
        self.thread.join(timeout=1)
        self.cap.release()


class DetectionWorker:
    """
    Menjalankan seluruh inferensi YOLO (objek + pose) di thread terpisah,
    berjalan berulang secepat yang model bisa. Loop tampilan (main thread)
    TIDAK PERNAH menunggu ini selesai -> video tetap mulus.
    """
    def __init__(self, video_stream):
        self.video_stream = video_stream
        self.lock = threading.Lock()
        self.stopped = False

        self.is_trash_detected = False
        self.gesture_ok = False
        self.littering_confirmed = False
        self._capture_frame = None

        self._debug_trash_boxes = []      # list of (box, tier)
        self._debug_trash_conf = []
        self._debug_kpts = None

        self._last_debug_print = 0

        # State trigger "sampah menetap"
        self._trash_present_since = None
        self._trash_last_seen = None
        self._trash_linger_triggered = False

        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _classify_detection(self, obj_name, conf_val, detect_conf):
        """Return tier ('definite'/'ambiguous') kalau obj_name dianggap sampah & lolos ambang, else None."""
        if obj_name in TRASH_CLASSES_DEFINITE:
            return 'definite'
        if obj_name in TRASH_CLASSES_AMBIGUOUS and conf_val >= (detect_conf + AMBIGUOUS_CONF_BONUS):
            return 'ambiguous'
        return None

    def _run(self):
        while not self.stopped:
            ret, frame = self.video_stream.read()
            if not ret or frame is None:
                time.sleep(0.01)
                continue

            # ============ 1. DETEKSI OBJEK (dengan TILING) ============
            seen_objects = []
            raw_detections = []  # list of (box, conf, tier)
            detect_conf = cfg.get('detection_confidence', 0.3)

            if ENABLE_TILING:
                h, w = frame.shape[:2]
                tiles = get_tiles(w, h)
            else:
                tiles = [(0, 0, frame.shape[1], frame.shape[0])]

            for (x1, y1, x2, y2) in tiles:
                crop = frame[y1:y2, x1:x2]
                if crop.size == 0:
                    continue

                # -- Model COCO umum (80 kelas generik) --
                res_obj = obj_model(crop, conf=detect_conf, imgsz=INFER_IMGSZ, verbose=False)
                for r in res_obj:
                    for box in r.boxes:
                        cls_id = int(box.cls)
                        obj_name = obj_model.names[cls_id]
                        conf_val = float(box.conf[0])
                        seen_objects.append((obj_name, conf_val))

                        tier = self._classify_detection(obj_name, conf_val, detect_conf)
                        if tier is not None:
                            bx = box.xyxy[0].cpu().numpy()
                            full_box = bx + [x1, y1, x1, y1]
                            raw_detections.append((full_box, conf_val, tier))

                # -- Model litter khusus (TACO) -- SEMUA kelasnya memang litter by definition,
                # jadi langsung dianggap tier 'litter' (paling relevan/dipercaya)
                if litter_model is not None:
                    res_litter = litter_model(crop, conf=cfg.get('litter_confidence', 0.35),
                                               imgsz=INFER_IMGSZ, verbose=False)
                    for r in res_litter:
                        for box in r.boxes:
                            cls_id = int(box.cls)
                            obj_name = f"litter:{litter_model.names[cls_id]}"
                            conf_val = float(box.conf[0])
                            seen_objects.append((obj_name, conf_val))

                            bx = box.xyxy[0].cpu().numpy()
                            full_box = bx + [x1, y1, x1, y1]
                            raw_detections.append((full_box, conf_val, 'litter'))

            merged = merge_tile_detections(raw_detections)
            trash_boxes = [m[0] for m in merged]
            trash_boxes_conf = [m[1] for m in merged]
            trash_boxes_tier = [m[2] for m in merged]

            if seen_objects:
                readable = ", ".join(f"{n}({c:.2f})" for n, c in seen_objects)
                print(f"AI Melihat: {readable}")

            is_trash_detected = len(trash_boxes) > 0

            # ---- TRIGGER "SAMPAH MENETAP": sampah terlihat terus > TRASH_LINGER_SECONDS ----
            now_ts = time.time()
            if is_trash_detected:
                if self._trash_present_since is None:
                    self._trash_present_since = now_ts
                    self._trash_linger_triggered = False
                self._trash_last_seen = now_ts
            else:
                if self._trash_last_seen is not None and (now_ts - self._trash_last_seen) > TRASH_GAP_TOLERANCE:
                    self._trash_present_since = None
                    self._trash_last_seen = None
                    self._trash_linger_triggered = False

            trash_linger_seconds = (now_ts - self._trash_present_since) if self._trash_present_since else 0.0
            trash_linger_confirmed = False
            if (trash_linger_seconds >= TRASH_LINGER_SECONDS) and not self._trash_linger_triggered:
                trash_linger_confirmed = True
                self._trash_linger_triggered = True
                print(f">>> KONFIRMASI: Sampah terlihat terus selama {trash_linger_seconds:.1f}s -> CAPTURE!")

            if DEBUG and is_trash_detected and time.time() - self._last_debug_print > 0.5:
                print(f"[DEBUG] sampah menetap selama {trash_linger_seconds:.1f}s (butuh {TRASH_LINGER_SECONDS:.0f}s)")

            # ============ 2. DETEKSI GESTUR (POSE) -- versi detail ============
            res_pose = pose_model(frame, conf=cfg.get('pose_confidence', 0.45),
                                   imgsz=INFER_IMGSZ, verbose=False)
            gesture_ok = False
            littering_confirmed = False
            person_found = False
            last_kpts = None

            for r in res_pose:
                if r.keypoints is None or r.keypoints.xy.numel() == 0:
                    continue
                kpts = r.keypoints.xy[0].cpu().numpy()
                if len(kpts) < 17:
                    continue
                person_found = True
                last_kpts = kpts

                nose = kpts[0]
                left_shoulder, right_shoulder = kpts[5], kpts[6]
                left_elbow, right_elbow = kpts[7], kpts[8]
                left_wrist, right_wrist = kpts[9], kpts[10]
                left_hip, right_hip = kpts[11], kpts[12]

                shoulder_mid = (left_shoulder + right_shoulder) / 2
                hip_mid = (left_hip + right_hip) / 2
                body_scale = max(
                    ((shoulder_mid[0] - hip_mid[0]) ** 2 + (shoulder_mid[1] - hip_mid[1]) ** 2) ** 0.5,
                    MIN_BODY_SCALE
                )

                def near_face(wrist):
                    d = ((wrist[0] - nose[0]) ** 2 + (wrist[1] - nose[1]) ** 2) ** 0.5
                    return d < FACE_EXCLUSION_RATIO * body_scale

                # --- Pola 1: OVERHAND (lempar ke atas/depan) ---
                right_overhand = (right_wrist[1] < right_shoulder[1] - HAND_ABOVE_SHOULDER_MARGIN * body_scale
                                   and right_elbow[1] < right_hip[1] - ELBOW_ABOVE_HIP_MARGIN * body_scale
                                   and not near_face(right_wrist))
                left_overhand = (left_wrist[1] < left_shoulder[1] - HAND_ABOVE_SHOULDER_MARGIN * body_scale
                                  and left_elbow[1] < left_hip[1] - ELBOW_ABOVE_HIP_MARGIN * body_scale
                                  and not near_face(left_wrist))

                # --- Pola 2: DROP (menjatuhkan ke bawah/samping) ---
                right_drop = (right_wrist[1] > right_hip[1] + HAND_BELOW_HIP_MARGIN * body_scale
                              and abs(right_wrist[0] - right_hip[0]) > HAND_SIDEWAYS_MARGIN * body_scale)
                left_drop = (left_wrist[1] > left_hip[1] + HAND_BELOW_HIP_MARGIN * body_scale
                             and abs(left_wrist[0] - left_hip[0]) > HAND_SIDEWAYS_MARGIN * body_scale)

                gesture_overhand = right_overhand or left_overhand
                gesture_drop = right_drop or left_drop
                gesture_ok = gesture_overhand or gesture_drop

                if DEBUG and time.time() - self._last_debug_print > 0.5:
                    self._last_debug_print = time.time()
                    print(f"[DEBUG] body_scale={body_scale:.0f}px | overhand(kanan={right_overhand},kiri={left_overhand}) "
                          f"| drop(kanan={right_drop},kiri={left_drop}) | sampah_terlihat={len(trash_boxes)}")

                if gesture_ok:
                    kind = "OVERHAND (lempar)" if gesture_overhand else "DROP (jatuhkan)"
                    print(f">>> GESTUR: Terdeteksi pola {kind}!")
                    if is_trash_detected:
                        littering_confirmed = True
                        print(">>> KONFIRMASI: Gestur + sampah terlihat -> BUANG SAMPAH SEMBARANGAN!")

            if DEBUG and not person_found and time.time() - self._last_debug_print > 0.5:
                self._last_debug_print = time.time()
                print("[DEBUG] Tidak ada orang/pose terdeteksi di frame ini.")

            littering_confirmed = littering_confirmed or trash_linger_confirmed

            with self.lock:
                self.is_trash_detected = is_trash_detected
                self.gesture_ok = gesture_ok
                self.littering_confirmed = littering_confirmed
                if littering_confirmed:
                    self._capture_frame = frame
                if DEBUG:
                    self._debug_trash_boxes = trash_boxes
                    self._debug_trash_conf = trash_boxes_conf
                    self._debug_trash_tier = trash_boxes_tier
                    self._debug_kpts = last_kpts

    def get_status(self):
        with self.lock:
            return self.is_trash_detected, self.gesture_ok, self.littering_confirmed

    def pop_capture_frame(self):
        with self.lock:
            f = self._capture_frame
            self._capture_frame = None
            return f

    def get_debug_data(self):
        with self.lock:
            return (self._debug_trash_boxes, self._debug_trash_conf,
                    getattr(self, '_debug_trash_tier', []), self._debug_kpts)

    def stop(self):
        self.stopped = True
        self.thread.join(timeout=1)


class SenderWorker:
    """
    Menyimpan foto ke disk & mengirim ke WhatsApp di thread TERPISAH.
    send_to_whatsapp() adalah request jaringan (HTTP) yang bisa makan waktu
    beberapa detik -- kalau dipanggil langsung di loop tampilan utama, video
    akan freeze/berhenti selama proses kirim berlangsung. Dengan antrian +
    thread ini, main loop cukup "titip" frame lalu lanjut jalan seperti biasa.
    """
    def __init__(self):
        self.q = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, frame):
        """Panggil ini dari main loop -- TIDAK blocking, cuma taruh di antrian."""
        self.q.put(frame)

    def _run(self):
        while True:
            frame = self.q.get()  
            try:
                path = "test_capture.jpg"
                cv2.imwrite(path, frame)
                print(" Mengirim foto ke WhatsApp...")
                success, msg = send_to_whatsapp(path, cfg['whatsapp_number'], cfg['openclaw_url'], cfg['openclaw_token'])
                print(f"Hasil Kirim: {msg}")
            except Exception as e:
                print(f"[ERROR] Gagal menyimpan/mengirim foto: {e}")
            finally:
                self.q.task_done()


def main():
    global last_sent
    print("\n--- MODE DIAGNOSTIK AKTIF ---")
    print("Mencoba membuka kamera...\n")

    try:
        cap = VideoStream(0)
    except RuntimeError as e:
        print(f" GAGAL MEMBUKA KAMERA:\n{e}")
        return

    print("AI akan mencetak apa pun yang dilihat ke terminal.\n")

    worker = DetectionWorker(cap)
    sender = SenderWorker()  # simpan+kirim foto jalan di thread sendiri, tidak memblokir video

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret or frame is None:
            continue

        is_trash_detected, gesture_ok, littering_confirmed = worker.get_status()

        capture_frame = worker.pop_capture_frame()
        if capture_frame is not None:
            now = time.time()
            if now - last_sent > cfg.get('cooldown_seconds', 5):
                print(" BUANG SAMPAH SEMBARANGAN TERDETEKSI! Mengambil Foto...")
                sender.submit(capture_frame)  # non-blocking -- simpan+kirim terjadi di background
                last_sent = now

        display_frame = frame
        if DEBUG:
            trash_boxes, trash_conf, trash_tier, kpts = worker.get_debug_data()
            for tbox, tconf, ttier in zip(trash_boxes, trash_conf, trash_tier):
                x1, y1, x2, y2 = [int(v) for v in tbox]
                if ttier == 'litter':
                    color = (0, 255, 0)      # hijau = dari model litter khusus (TACO), paling dipercaya
                elif ttier == 'definite':
                    color = (0, 165, 255)    # oranye = kelas COCO definite
                else:
                    color = (0, 255, 255)    # kuning = kelas COCO ambiguous
                cv2.rectangle(display_frame, (x1, y1), (x2, y2), color, 2)
                cv2.putText(display_frame, f"{ttier} {tconf:.2f}", (x1, max(0, y1 - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
            if kpts is not None:
                for (kx, ky) in kpts:
                    cv2.circle(display_frame, (int(kx), int(ky)), 3, (255, 0, 0), -1)

        status_text = f"Sampah: {is_trash_detected} | Gestur: {gesture_ok} | Konfirmasi: {littering_confirmed}"
        color = (0, 255, 0) if littering_confirmed else (0, 0, 255)
        cv2.putText(display_frame, status_text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        cv2.imshow("Diagnostik", display_frame)

        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    worker.stop()
    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()