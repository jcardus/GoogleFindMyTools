#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
import undetected_chromedriver as uc
import os
import shutil
import platform
import re
import subprocess
import time

def find_chrome():
    """Find Chrome executable using known paths and system commands."""
    possiblePaths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\ProgramData\chocolatey\bin\chrome.exe",
        r"C:\Users\%USERNAME%\AppData\Local\Google\Chrome\Application\chrome.exe",
        "/usr/bin/google-chrome",
        "/usr/local/bin/google-chrome",
        "/opt/google/chrome/chrome",
        "/snap/bin/chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    ]
    # Check predefined paths
    for path in possiblePaths:
        path = os.path.expandvars(path)
        if os.path.exists(path):
            return path
    # Use system command to find Chrome
    try:
        if platform.system() == "Windows":
            chrome_path = shutil.which("chrome")
        else:
            chrome_path = shutil.which("google-chrome") or shutil.which("chromium")
        if chrome_path:
            return chrome_path
    except Exception as e:
        print(f"[ChromeDriver] Error while searching system paths: {e}")
    return None

def get_chrome_major_version(chrome_path):
    """Return the installed Chrome major version, if it can be detected."""
    if not chrome_path:
        return None

    try:
        output = subprocess.check_output(
            [chrome_path, "--version"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
        match = re.search(r"(\d+)\.", output)
        if match:
            return int(match.group(1))
    except Exception as e:
        print(f"[ChromeDriver] Could not detect Chrome version from {chrome_path}: {e}")

    return None

def get_options():
    chrome_options = uc.ChromeOptions()
    chrome_options.add_argument("--start-maximized")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    return chrome_options

def create_driver():
    """Create a Chrome WebDriver with undetected_chromedriver."""
    chrome_path = find_chrome()
    chrome_major_version = get_chrome_major_version(chrome_path)
    if chrome_major_version:
        print(f"[ChromeDriver] Detected Chrome major version: {chrome_major_version}")
    else:
        print("[ChromeDriver] Could not detect Chrome major version; using ChromeDriver default.")

    try:
        # Kill any existing Chrome processes first
        try:
            if platform.system() == "Windows":
                os.system("taskkill /f /im chrome.exe >nul 2>&1")
            else:
                os.system("pkill -f chrome")
            time.sleep(2)  # Wait for processes to close
        except:
            pass
            
        chrome_options = get_options()
        if chrome_path:
            chrome_options.binary_location = chrome_path
        driver = uc.Chrome(options=chrome_options, version_main=chrome_major_version)
        print("[ChromeDriver] Installed and browser started.")
        return driver
    except Exception as e:
        print(f"[ChromeDriver] Default ChromeDriver creation failed: {e}")
        print("[ChromeDriver] Trying alternative paths...")
        if chrome_path:
            chrome_options = get_options()
            chrome_options.binary_location = chrome_path
            try:
                driver = uc.Chrome(options=chrome_options, version_main=chrome_major_version)
                print(f"[ChromeDriver] ChromeDriver started using {chrome_path}")
                return driver
            except Exception as e:
                print(f"[ChromeDriver] ChromeDriver failed using path {chrome_path}: {e}")
        else:
            print("[ChromeDriver] No Chrome executable found in known paths.")
        
        # Final fallback - try headless mode
        print("[ChromeDriver] Trying headless mode as last resort...")
        try:
            chrome_options = get_options()
            chrome_options.add_argument("--headless")
            if chrome_path:
                chrome_options.binary_location = chrome_path
            driver = uc.Chrome(options=chrome_options, version_main=chrome_major_version)
            print("[ChromeDriver] Started in headless mode successfully.")
            return driver
        except Exception as e:
            print(f"[ChromeDriver] Headless mode also failed: {e}")
        
        raise Exception(
            "[ChromeDriver] Failed to install ChromeDriver. A current version of Chrome was not detected on your system.\n"
            "If you know that Chrome is installed, update Chrome to the latest version. If the script is still not working, "
            "set the path to your Chrome executable manually inside the script."
        )

if __name__ == '__main__':
    create_driver()


def safe_quit_driver(driver):
    if driver is None:
        return

    try:
        driver.quit()
    except OSError:
        pass
    except Exception:
        pass

    # undetected_chromedriver calls quit() again from Chrome.__del__.
    # On Windows that can emit "Exception ignored ... WinError 6" after a
    # successful run because the handle is already closed.
    try:
        driver.quit = lambda: None
    except Exception:
        pass
