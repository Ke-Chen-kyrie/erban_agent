import time
import threading
from pathlib import Path

import cv2
import numpy as np
import pyaudio
from scipy.signal import resample_poly

from IdentificationClient.client import IdentificationClient


IDENTIFICATION_URL = "http://0.0.0.0:8001"


# ═══════════════════════════════════════════════════════════════════════
# 设备查找
# ═══════════════════════════════════════════════════════════════════════

def find_device_by_name(pa, keyword, is_input=True):
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        name = info["name"].lower()
        if keyword.lower() in name:
            if is_input and info["maxInputChannels"] > 0:
                return info["index"]
            if not is_input and info["maxOutputChannels"] > 0:
                return info["index"]
    return None


# ═══════════════════════════════════════════════════════════════════════
# 摄像头采集
# ═══════════════════════════════════════════════════════════════════════

class CameraCapture:
    def __init__(self, camera_index: int = 0, width: int = 640, height: int = 480):
        self.camera_index = camera_index
        self.width = width
        self.height = height
        self._cap = None

    def open(self):
        self._cap = cv2.VideoCapture(self.camera_index)
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        if not self._cap.isOpened():
            raise RuntimeError(f"无法打开摄像头 (index={self.camera_index})")

    def close(self):
        if self._cap is not None:
            self._cap.release()
            cv2.destroyAllWindows()
            self._cap = None

    def capture_photo(self, save_path: str | Path | None = None) -> bytes:
        if self._cap is None:
            self.open()
        for _ in range(10):
            self._cap.read()
        ret, frame = self._cap.read()
        if not ret:
            raise RuntimeError("摄像头拍照失败")
        _, jpg = cv2.imencode(".jpg", frame)
        if save_path is not None:
            Path(save_path).parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(save_path), frame)
        return jpg.tobytes()

    def preview_and_capture(
        self,
        prompt: str = "按 SPACE 拍照，Q 退出",
        save_path: str | Path | None = None,
    ) -> bytes | None:
        self.open()
        print(f"📷 {prompt}")
        result = None
        while True:
            ret, frame = self._cap.read()
            if not ret:
                break
            display = frame.copy()
            cv2.putText(display, prompt, (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            cv2.imshow("Camera", display)
            key = cv2.waitKey(1) & 0xFF
            if key == ord(" "):
                _, jpg = cv2.imencode(".jpg", frame)
                if save_path is not None:
                    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
                    cv2.imwrite(str(save_path), frame)
                    print(f"   已保存: {save_path}")
                result = jpg.tobytes()
                break
            elif key == ord("q"):
                break
        self.close()
        return result


# ═══════════════════════════════════════════════════════════════════════
# 麦克风采集
# ═══════════════════════════════════════════════════════════════════════

class AudioCapture:
    TARGET_RATE = 16000
    PREFERRED_DEVICES = ["Wireless Mic Rx", "DJI"]  # 按优先级排列

    def __init__(self, chunk_size=1600, device_index=None, device_name=None):
        self.chunk_size = chunk_size
        self.audio_queue = None
        self.is_running = threading.Event()
        self._paused = threading.Event()
        self.p = pyaudio.PyAudio()

        if device_name is not None:
            idx = find_device_by_name(self.p, device_name, is_input=True)
            if idx is not None:
                device_index = idx
            else:
                print(f"⚠️ 找不到麦克风设备 (关键词: {device_name})，使用默认设备")

        if device_index is None:
            for keyword in self.PREFERRED_DEVICES:
                idx = find_device_by_name(self.p, keyword, is_input=True)
                if idx is not None:
                    device_index = idx
                    break

        if device_index is not None:
            info = self.p.get_device_info_by_index(device_index)
            self.device_index = device_index
            self.device_name = info["name"]
            self.device_rate = int(info["defaultSampleRate"])
            self.device_channels = info["maxInputChannels"]
        else:
            default_info = self.p.get_default_input_device_info()
            self.device_index = default_info["index"]
            self.device_name = default_info["name"]
            self.device_rate = int(default_info["defaultSampleRate"])
            self.device_channels = default_info["maxInputChannels"]

        self.capture_channels = min(1, self.device_channels)
        if self.capture_channels < 1:
            raise ValueError(f"设备 {self.device_name} 不支持输入")

        ratio = self.device_rate / self.TARGET_RATE
        self.chunk_size = int(chunk_size * ratio)
        print(f"🎙️ 录音设备: {self.device_name} (索引 {self.device_index}, 原生 {self.device_rate} Hz → 输出 {self.TARGET_RATE} Hz)")

    def pause(self):
        self._paused.set()

    def resume(self):
        self._paused.clear()

    def start(self, audio_queue):
        self.audio_queue = audio_queue
        self.is_running.set()
        self.thread = threading.Thread(target=self._record_loop, daemon=True)
        self.thread.start()
        print(f"🎙️ 音频采集已启动 (chunk_size {self.chunk_size})")
        return True

    def stop(self):
        self.is_running.clear()
        if hasattr(self, "thread"):
            self.thread.join(timeout=2)
        self.p.terminate()

    def _record_loop(self):
        need_resample = (self.device_rate != self.TARGET_RATE)
        stream = self.p.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self.device_rate,
            input=True,
            input_device_index=self.device_index,
            frames_per_buffer=self.chunk_size,
        )
        try:
            while self.is_running.is_set():
                try:
                    data = stream.read(self.chunk_size, exception_on_overflow=False)
                    if self._paused.is_set():
                        continue
                    if self.audio_queue is not None and not self.audio_queue.full():
                        audio_np = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                        if need_resample:
                            audio_np = resample_poly(audio_np, self.TARGET_RATE, self.device_rate)
                        self.audio_queue.put(audio_np)
                except Exception as e:
                    if self.is_running.is_set():
                        print(f"录音读取异常: {e}")
                    break
        finally:
            stream.stop_stream()
            stream.close()

    def record_to_file(self, duration: float, save_path: str | Path) -> Path:
        import queue
        import wave

        save_path = Path(save_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)

        q: queue.Queue = queue.Queue()
        chunk_duration = self.chunk_size / self.device_rate
        num_chunks = int(duration / chunk_duration) + 1

        self.start(q)
        print(f"🔴 开始录制 {duration} 秒...")

        frames: list[np.ndarray] = []
        for _ in range(num_chunks):
            try:
                chunk = q.get(timeout=duration + 2)
                frames.append(chunk)
            except queue.Empty:
                break
        self.stop()

        audio_data = np.concatenate(frames) if frames else np.array([], dtype=np.float32)
        int_data = (audio_data * 32767).astype(np.int16)

        with wave.open(str(save_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.TARGET_RATE)
            wf.writeframes(int_data.tobytes())

        print(f"✅ 已保存: {save_path} ({len(int_data) / self.TARGET_RATE:.1f}s)")
        return save_path

    @staticmethod
    def play_file(file_path: str | Path) -> None:
        import wave
        p = pyaudio.PyAudio()
        try:
            with wave.open(str(file_path), "rb") as wf:
                stream = p.open(
                    format=p.get_format_from_width(wf.getsampwidth()),
                    channels=wf.getnchannels(),
                    rate=wf.getframerate(),
                    output=True,
                )
                data = wf.readframes(1024)
                while data:
                    stream.write(data)
                    data = wf.readframes(1024)
                stream.stop_stream()
                stream.close()
            print(f"🔊 播放完毕: {file_path}")
        finally:
            p.terminate()


# ═══════════════════════════════════════════════════════════════════════
# 测试接口
# ═══════════════════════════════════════════════════════════════════════

class Tester:
    def __init__(self, base_url: str = IDENTIFICATION_URL):
        self.api = IdentificationClient(base_url)
        self._camera = None
        self._mic = None

    @property
    def camera(self):
        if self._camera is None:
            self._camera = CameraCapture()
        return self._camera

    @property
    def mic(self):
        if self._mic is None:
            self._mic = AudioCapture()
        return self._mic

    def test_health(self):
        print("\n" + "=" * 50)
        print("  测试: 健康检查")
        print("=" * 50)
        result = self.api.health()
        print(f"  响应: {result}")
        return result

    def test_register(self, user_id: str, name: str, role: str = "", description: str = ""):
        print("\n" + "=" * 50)
        print(f"  测试: 注册用户 (id={user_id}, name={name}, role={role}, description={description})")
        print("=" * 50)

        face_path = f"/tmp/test_face_{user_id}.jpg"
        jpg = self.camera.preview_and_capture(
            prompt=f"注册 {name} - 按 SPACE 拍照",
            save_path=face_path,
        )
        if jpg is None:
            print("  ❌ 用户取消拍照")
            return None

        audio_path = f"/tmp/test_audio_{user_id}.wav"
        self.mic.record_to_file(duration=15, save_path=audio_path)

        print(f"  🔊 播放录入的音频...")
        AudioCapture.play_file(audio_path)

        print(f"  📤 调用注册接口...")
        result = self.api.register(user_id, name, face_path, audio_path, role=role, description=description)
        print(f"  响应: {result}")
        return result

    def test_face_search(self):
        print("\n" + "=" * 50)
        print("  测试: 人脸搜索")
        print("=" * 50)

        face_path = "/tmp/test_face_search.jpg"
        jpg = self.camera.preview_and_capture(
            prompt="人脸搜索 - 按 SPACE 拍照",
            save_path=face_path,
        )
        if jpg is None:
            print("  ❌ 用户取消拍照")
            return None

        print(f"  📤 调用人脸搜索接口...")
        result = self.api.face_search(face_path, top_k=3)
        print(f"  request_id: {result.get('request_id', '')}")
        items = result.get("results", [])
        if not items:
            print("  (无匹配结果)")
        else:
            for r in items:
                print(f"    entity_id={r['entity_id']}  face_id={r['face_id']}  score={r['score']:.4f}  confidence={r['confidence']:.2f}")
        return result

    def test_voice_search(self):
        print("\n" + "=" * 50)
        print("  测试: 声纹搜索")
        print("=" * 50)

        audio_path = "/tmp/test_voice_search.wav"
        self.mic.record_to_file(duration=3, save_path=audio_path)

        print(f"  📤 调用声纹搜索接口...")
        result = self.api.voice_search(audio_path, top_k=1)
        print(f"  响应: {result}")
        return result

    def test_face_verify(self, user_id: str):
        print("\n" + "=" * 50)
        print(f"  测试: 人脸验证 (id={user_id})")
        print("=" * 50)

        face_path = "/tmp/test_face_verify.jpg"
        jpg = self.camera.preview_and_capture(
            prompt=f"验证 {user_id} - 按 SPACE 拍照",
            save_path=face_path,
        )
        if jpg is None:
            print("  ❌ 用户取消拍照")
            return None

        print(f"  📤 调用人脸验证接口...")
        result = self.api.face_verify(user_id, face_path)
        status = "✅ 匹配" if result.get("match") else "❌ 不匹配"
        print(f"  响应: {status} (score={result.get('score', 0):.4f}, confidence={result.get('confidence', 0):.2f}, threshold={result.get('threshold')})")
        return result

    def test_face_detect(self):
        print("\n" + "=" * 50)
        print("  测试: 人脸检测与识别")
        print("=" * 50)

        face_path = "/tmp/test_face_detect.jpg"
        jpg = self.camera.preview_and_capture(
            prompt="人脸检测 - 按 SPACE 拍照",
            save_path=face_path,
        )
        if jpg is None:
            print("  ❌ 用户取消拍照")
            return None

        print(f"  📤 调用人脸检测接口...")
        result = self.api.face_detect(face_path)
        print(f"  request_id: {result.get('request_id', '')}")
        print(f"  共检测到 {result.get('total_faces', 0)} 张人脸 (匹配 {result.get('matched_count', 0)}, 未知 {result.get('unknown_count', 0)})")
        for f in result.get("faces", []):
            loc = f.get("location") or {}
            status = "✅" if f["matched"] else "❓"
            print(f"    {status} user_id={f['user_id']}  name={f.get('name', '')}  score={f['score']:.4f}  "
                  f"location=({loc.get('x')}, {loc.get('y')}, {loc.get('width')}x{loc.get('height')})  "
                  f"mouth_points={len(f.get('mouth', []))}")

        # 在图片上框出人脸
        img = cv2.imread(face_path)
        if img is not None:
            for f in result.get("faces", []):
                loc = f.get("location")
                if not loc:
                    continue
                x, y, w, h = loc["x"], loc["y"], loc["width"], loc["height"]
                color = (0, 255, 0) if f["matched"] else (0, 0, 255)
                cv2.rectangle(img, (x, y), (x + w, y + h), color, 2)
                label = f['user_id'] if f["matched"] else f"unknown"
                if f.get("name"):
                    label = f"{f['name']}({f['user_id']})"
                cv2.putText(img, label, (x, y - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

                # 画嘴巴关键点
                mouth = f.get("mouth", [])
                for p in mouth:
                    px, py = int(p["x"]), int(p["y"])
                    cv2.circle(img, (px, py), 2, (255, 0, 0), -1)

            cv2.imshow("Face Detect Result", img)
            print("  按任意键关闭图片窗口...")
            cv2.waitKey(0)
            cv2.destroyAllWindows()
        return result

    def test_get_face(self, user_id: str):
        print("\n" + "=" * 50)
        print(f"  测试: 获取用户人脸 (id={user_id})")
        print("=" * 50)
        result = self.api.get_user_face(user_id)
        face_b64 = result.get("face_image")
        print(f"  响应: user_id={result.get('user_id')}, name={result.get('name')}")

        if face_b64 and face_b64.startswith("data:image"):
            import base64
            header, encoded = face_b64.split(",", 1)
            img_bytes = base64.b64decode(encoded)
            img = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                cv2.imshow(f"Face: {user_id}", img)
                print("  按任意键关闭图片窗口...")
                cv2.waitKey(0)
                cv2.destroyAllWindows()
            else:
                print("  ⚠️ 无法解码图片")
        else:
            print(f"  ⚠️ 无人脸图片 (face_image={face_b64})")
        return result

    def test_get_user_info(self, user_id: str):
        print("\n" + "=" * 50)
        print(f"  测试: 查询用户信息 (id={user_id})")
        print("=" * 50)
        result = self.api.get_user_info(user_id)
        if result.get("exists"):
            print(f"  id={result['user_id']}  name={result['name']}  role={result.get('role', '')}  created={result.get('created_at', '')}")
        else:
            print(f"  用户不存在: id={result['user_id']}")
        return result

    def test_delete(self, user_id: str):
        print("\n" + "=" * 50)
        print(f"  测试: 删除用户 (id={user_id})")
        print("=" * 50)
        result = self.api.delete_user(user_id)
        print(f"  响应: {result}")
        return result

    def test_list_users(self):
        print("\n" + "=" * 50)
        print("  测试: 用户列表")
        print("=" * 50)
        result = self.api.list_users()
        print(f"  共 {result.get('total', 0)} 个用户:")
        for u in result.get("users", []):
            print(f"    id={u['user_id']}  name={u['name']}  role={u.get('role', '')}  description={u.get('description', '')}  created={u.get('created_at', '')}")
        return result

    def test_sync(self):
        print("\n" + "=" * 50)
        print("  测试: 同步云端与本地数据")
        print("=" * 50)
        print("  检查云端声纹/人脸数据，删除本地不存在的用户...")
        result = self.api.sync()
        voice_cleanup = result.get("voice_cleanup", [])
        face_cleanup = result.get("face_cleanup", [])
        if voice_cleanup:
            print(f"  已删除云端声纹: {voice_cleanup}")
        else:
            print("  无声纹孤儿数据")
        if face_cleanup:
            print(f"  已删除云端人脸: {face_cleanup}")
        else:
            print("  无人脸孤儿数据")
        print(f"  完整响应: {result}")
        return result

    def run_all(self, user_id: str = "test_user", name: str = "测试用户", role: str = ""):
        self.test_health()

        print("\n" + "-" * 50)
        ans = input("继续测试注册？[y/N] ").strip().lower()
        if ans == "y":
            self.test_register(user_id, name, role)

        print("\n" + "-" * 50)
        ans = input("继续测试人脸搜索？[y/N] ").strip().lower()
        if ans == "y":
            self.test_face_search()

        print("\n" + "-" * 50)
        ans = input("继续测试人脸检测？[y/N] ").strip().lower()
        if ans == "y":
            self.test_face_detect()

        print("\n" + "-" * 50)
        ans = input("继续测试声纹搜索？[y/N] ").strip().lower()
        if ans == "y":
            self.test_voice_search()

        print("\n" + "-" * 50)
        ans = input(f"查看用户 {user_id} 的人脸？[y/N] ").strip().lower()
        if ans == "y":
            self.test_get_face(user_id)

        print("\n" + "-" * 50)
        ans = input(f"1:1 人脸验证 {user_id}？[y/N] ").strip().lower()
        if ans == "y":
            self.test_face_verify(user_id)

        print("\n" + "-" * 50)
        ans = input(f"删除用户 {user_id}？[y/N] ").strip().lower()
        if ans == "y":
            self.test_delete(user_id)

        print("\n" + "-" * 50)
        ans = input("查看用户列表？[y/N] ").strip().lower()
        if ans == "y":
            self.test_list_users()

        print("\n✅ 测试完成")


# ═══════════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    tester = Tester()

    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "register":
            uid = sys.argv[2] if len(sys.argv) > 2 else "u001"
            name = sys.argv[3] if len(sys.argv) > 3 else "用户"
            role = sys.argv[4] if len(sys.argv) > 4 else ""
            description = sys.argv[5] if len(sys.argv) > 5 else ""
            tester.test_register(uid, name, role, description)
        elif cmd == "face":
            tester.test_face_search()
        elif cmd == "voice":
            tester.test_voice_search()
        elif cmd == "getface":
            uid = sys.argv[2] if len(sys.argv) > 2 else "u001"
            tester.test_get_face(uid)
        elif cmd == "delete":
            uid = sys.argv[2] if len(sys.argv) > 2 else "u001"
            tester.test_delete(uid)
        elif cmd == "health":
            tester.test_health()
        elif cmd == "list_user":
            tester.test_list_users()
        elif cmd == "verify":
            uid = sys.argv[2] if len(sys.argv) > 2 else "u001"
            tester.test_face_verify(uid)
        elif cmd == "info":
            uid = sys.argv[2] if len(sys.argv) > 2 else "u001"
            tester.test_get_user_info(uid)
        elif cmd == "detect":
            tester.test_face_detect()
        elif cmd == "sync":
            tester.test_sync()
        else:
            print(f"未知命令: {cmd}")
            print("可用: health | register [user_id] [name] [role] [description] | face | voice | verify [user_id] | detect | getface [user_id] | info [user_id] | delete [user_id] | list_user | sync")
    else:
        tester.run_all()