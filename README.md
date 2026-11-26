# CUKTECH 10 Ultra Windows Monitor

Ứng dụng Windows theo dõi và điều khiển củ sạc **CUKTECH 10 GaN Charger Ultra** (`njcuk.fitting.ad1204`) qua Bluetooth Low Energy (BLE), hỗ trợ:

- **Giám sát thời gian thực**: Tổng công suất (0–120W), thông số V / A / W và giao thức (`PD`, `PPS`, `UFCS`, `SCP`, `QC`) của cả 4 cổng (`USB-C1`, `USB-C2`, `USB-C3`, `USB-A`), biểu đồ công suất 3 phút.
- **Điều khiển trực tiếp**: Bật/tắt từng cổng, chế độ phân bổ công suất (`AI`, `Apple`, `Single`, `Balance`), thời gian tắt màn hình, ngôn ngữ, khóa xoay, USB-A dòng nhỏ, công tắc giao thức từng cổng.
- **Liquid Glass Floating Widget**: Widget kính mờ luôn nổi trên màn hình Windows 11 (`Acrylic Blur`), kéo thả tự do, chuyển đổi 1 chạm giữa Widget và App đầy đủ.
- **Khởi động cùng Windows**: Tùy chọn tự mở Widget khi bật máy.
- **Đăng nhập Mi Home bằng mã QR**: Tự động lấy `MAC`, `Token` và `BLE Key` từ tài khoản Xiaomi Cloud.
- **Lịch sử SQLite & Xuất CSV**: Lưu các phiên sạc tự động.

## Chạy từ mã nguồn

```powershell
python -m venv .venv
.\.venv\Scripts\pip install bleak pywebview cryptography pycryptodomex requests aiohttp pillow qrcode
.\CuktechMonitor.bat
# Hoặc mở thẳng Widget:
.\CuktechWidget.bat
```

## Đóng gói `.exe`

```powershell
.\.venv\Scripts\pyinstaller --noconfirm --onefile --windowed --name CuktechMonitor --add-data "ui;ui" --hidden-import winrt.windows.devices.bluetooth --hidden-import winrt.windows.devices.bluetooth.advertisement --hidden-import winrt.windows.devices.bluetooth.genericattributeprofile --hidden-import winrt.windows.devices.enumeration --hidden-import winrt.windows.storage.streams --hidden-import Cryptodome.Cipher.AES main.py
```

## Ghi công

Phần giao thức BLE MiOT và Xiaomi Cloud QR dựa trên mã nguồn mở [kairui1108/cuktech-ble-server](https://github.com/kairui1108/cuktech-ble-server).
