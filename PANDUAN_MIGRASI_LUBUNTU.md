# Panduan Migrasi Harvey ke Lubuntu (Server Sales 24/7 Otomatis)

Panduan lengkap menjalankan Harvey di laptop Lubuntu (atau Ubuntu Server / VPS) agar beroperasi **24 jam non-stop**, hemat listrik, mandiri, dan otomatis menyala kembali jika laptop/server restart.

---

## 📦 Bagian 1: Memindahkan File ke Lubuntu

Kami telah membundel seluruh sistem Harvey versi terbaru (lengkap dengan perbaikan SPH Chaser, Behavioral Guidelines, Sourcing Engine, dan sesi WhatsApp aktif) ke dalam satu file arsip:

### **`harvey_lubuntu.zip`**

1. Salin file **`harvey_lubuntu.zip`** ke flashdisk (atau upload via SCP/Google Drive ke laptop Lubuntu).
2. Di Lubuntu, ekstrak file zip tersebut ke folder Home:
   ```bash
   # Masuk ke folder Home
   cd ~
   
   # Ekstrak file zip
   unzip harvey_lubuntu.zip -d ~/Harvey
   ```

> [!NOTE]
> File zip ini sudah menyertakan:
> * Sesi login WhatsApp Web (`data/whatsapp_profile/`) sehingga **tidak perlu scan QR ulang**.
> * Database penawaran SPH yang sudah bersih & tervalidasi (`data/harvey.db`).
> * Seluruh aturan perilaku konsultatif AndisLab (`data/guidelines.json`).
> * Skrip instalasi otomatis 1-klik (`setup_lubuntu.sh`).

---

## 🚀 Bagian 2: Instalasi Otomatis 1-Klik di Lubuntu

Buka Terminal Lubuntu (**`Ctrl + Alt + T`**), lalu jalankan perintah berikut:

```bash
cd ~/Harvey
chmod +x setup_lubuntu.sh start_dashboard.sh
bash setup_lubuntu.sh
```

**Skrip `setup_lubuntu.sh` akan secara otomatis:**
1. Menginstall paket sistem pendukung (Python 3, venv, pip, curl).
2. Membuat Virtual Environment Linux (`.venv`).
3. Menginstall Harvey dan seluruh modulnya.
4. Mengunduh browser Chromium Linux & dependensinya untuk WhatsApp Web.
5. Memasang **`systemd service`** bernama `harvey.service` agar Harvey otomatis jalan di latar belakang (*background daemon*) setiap kali laptop Lubuntu dinyalakan.

---

## 🌙 Bagian 3: Setting Laptop Agar Tetap Jalan Saat Layar Ditutup

Agar laptop Lubuntu bisa diletakkan di sudut meja dengan layar tertutup tanpa masuk mode tidur (sleep/suspend):

1. Buka file konfigurasi logind:
   ```bash
   sudo nano /etc/systemd/logind.conf
   ```
2. Cari baris `#HandleLidSwitch=suspend`, hapus tanda `#` dan sesuaikan menjadi:
   ```ini
   HandleLidSwitch=ignore
   HandleLidSwitchExternalPower=ignore
   ```
3. Simpan dengan menekan **`Ctrl + O`** $\rightarrow$ **`Enter`**, lalu keluar dengan **`Ctrl + X`**.
4. Terapkan konfigurasi:
   ```bash
   sudo systemctl restart systemd-logind
   ```

---

## 🎮 Bagian 4: Perintah Penting Sehari-hari

Harvey sudah berjalan otomatis di latar belakang. Anda bisa memantau dan mengontrolnya dengan perintah berikut di Terminal:

### 1. Melihat Aktivitas Sales Secara Real-Time (Live Stream Log)
```bash
journalctl -u harvey -f
```
*(Tekan `Ctrl + C` untuk keluar dari tampilan log).*

### 2. Cek Status Apakah Harvey Sedang Berjalan
```bash
sudo systemctl status harvey
```

### 3. Menghentikan Sementara Harvey
```bash
sudo systemctl stop harvey
```

### 4. Menjalankan Kembali Harvey
```bash
sudo systemctl start harvey
```

### 5. Restart Harvey (setelah edit file/konfigurasi)
```bash
sudo systemctl restart harvey
```

### 6. Membuka Dashboard Web Harvey di Lubuntu
```bash
cd ~/Harvey
./start_dashboard.sh
```
Lalu buka browser Lubuntu di alamat: **`http://127.0.0.1:5555`**

---

## 🛡️ Keamanan & Kestabilan Sistem di Lubuntu

1. **Anti-BSOD / Anti-Crash:** Di sistem Linux tidak ada driver TDR Windows yang bentrok. Playwright Chromium berjalan dengan parameter `CHROME_SAFE_ARGS` yang ringan RAM dan stabil.
2. **Auto-Recovery:** Jika terjadi gangguan internet atau laptop ter-restart, sistem `systemd` akan otomatis menghidupkan kembali Harvey dalam 15 detik.
3. **Penyimpanan Lokal:** Seluruh data penawaran, chat, dan riwayat pelanggan tetap berada di penyimpanan lokal laptop Anda (`data/harvey.db`), aman dan tanpa biaya server bulanan tambahan.
