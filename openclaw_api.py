import subprocess
import os

def send_to_whatsapp(image_path, wa_number, url, token):
    """Mengirim gambar menggunakan perintah CLI OpenClaw yang sudah disesuaikan"""
    
    abs_image_path = os.path.abspath(image_path)
    

    ps_command = (
        f'openclaw message send '
        f'--target "{wa_number}" '
        f'--channel "whatsapp" '
        f'--media "{abs_image_path}" '
        f'--message "terdeteksi bahsa di daerah tersebut terdapat sampah⚠️ "'
    )
    
    try:
        print(f"Sedang mencoba mengirim via PowerShell (Target: {wa_number})...")
        
        result = subprocess.run(
            ["powershell", "-Command", ps_command], 
            capture_output=True, 
            text=True, 
            encoding='utf-8'
        )
        
        if result.returncode == 0:
            return True, "Berhasil dikirim melalui PowerShell!"
        else:
            error_msg = result.stderr if result.stderr else result.stdout
            return False, f"CLI Error: {error_msg.strip()}"
            
    except Exception as e:
        return False, f"Gagal menjalankan CLI: {str(e)}"
