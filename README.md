### How to use

> [!CAUTION]
> Before starting, ensure you have Chrome updated and Python 3.12 installed. Other Python versions are not supported for this repo.
> 
> **If Chrome is not up to date, the script will NOT work, guaranteed!**

- Clone this repository: `git clone` or download the ZIP file
- Change into the directory: `cd GoogleFindMyTools`
- Use the Python version pinned in [.python-version](.python-version), currently Python 3.12.
- Optional: Create venv: `py -3.12 -m venv venv` (Windows) or `python3 -m venv venv` (Linux & macOS)
- Optional: Activate venv: `venv\Scripts\activate` (Windows) or `source venv/bin/activate` (Linux & macOS)
- Install all required packages: `python -m pip install -r requirements.txt`
- Install the latest version of Google Chrome: https://www.google.com/chrome/
- Start the program by running [main.py](main.py): `python main.py` or `python3 main.py`

### Register your Google account with Tagora

Use these steps if someone sent you this repository so you can connect a Google account to a Tagora hub.

Before you start, you need:

- Python 3.12
- The latest Google Chrome
- A Google account that can sign in on this computer
- A `PROVISIONING_TOKEN` from the Tagora hub owner

On Windows:

```powershell
git clone https://github.com/jcardus/GoogleFindMyTools.git
cd GoogleFindMyTools
py -3.12 -m venv venv
venv\Scripts\activate
python -m pip install -r requirements.txt
python provision_google_account.py
```

On macOS or Linux:

```bash
git clone https://github.com/jcardus/GoogleFindMyTools.git
cd GoogleFindMyTools
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python provision_google_account.py
```

When prompted for `PROVISIONING_TOKEN`, paste the token from the Tagora hub owner. The script removes the local auth cache, opens Chrome for Google login, asks for any required Find Hub E2EE approval, then uploads the fresh cache to the Tagora backend.

To upload an existing cache without logging in again, run:

```bash
python provision_google_account.py --use-existing-auth
```

### Authentication

On the first run, an authentication sequence is executed, which requires a computer with access to Google Chrome.

The authentication results are stored in `Auth/secrets.json`. If you intend to run this tool on a headless machine, you can just copy this file to avoid having to use Chrome.

To create or refresh the auth cache directly, run `python provision_account_auth.py`. For hub deployments, set `PROVISIONING_TOKEN` and run `python provision_google_account.py`; this removes the local auth cache, logs in again, uploads the fresh cache, and registers the account through the Tagora backend. To upload an existing cache without logging in again, run `python provision_google_account.py --use-existing-auth`.

For multi-account hub deployments, `microservice.py --google-account account@example.com` reads `Auth/account@example.com.json` by default. You can store those per-account files in Cloudflare R2 by setting `GOOGLE_SECRETS_R2_BUCKET`, `GOOGLE_SECRETS_R2_ACCOUNT_ID`, `GOOGLE_SECRETS_R2_ACCESS_KEY_ID`, and `GOOGLE_SECRETS_R2_SECRET_ACCESS_KEY`. When R2 is configured, each account is stored at `google-secrets/<google-account>.json` unless `GOOGLE_SECRETS_R2_PREFIX` or `GOOGLE_SECRETS_R2_KEY` overrides it.

### Known Issues
- "Your encryption data is locked on your device" is shown if you have never set up Find My Device on an Android device. Solution: Login with your Google Account on an Android device, go to Settings > Google > All Services > Find My Device > Find your offline devices > enable "With network in all areas" or "With network in high-traffic areas only". If "Find your offline devices" is not shown in Settings, you will need to download the Find My Device app from Google's Play Store, and pair a real Find My Device tracker with your device to force-enable the Find My Device network.
- No support for trackers using the P-256 curve and 32-Byte advertisements. Regular trackers don't seem to use this curve at all - I can only confirm that it is used with Sony's WH1000XM5 headphones.
- No support for the authentication process on ARM Linux
- If you receive "ssl.SSLCertVerificationError" when running the script, try to follow [this answer](https://stackoverflow.com/a/53310545).
- Please also consider the issues listed in the [README in the ESP32Firmware folder](ESP32Firmware/README.md) if you want to register custom trackers.

### Firmware for custom ESP32-based trackers
If you want to use an ESP32 as a custom Find My Device tracker, you can find the firmware in the folder ESP32Firmware. To register a new tracker, run main.py and press 'r' if you are asked to. Afterward, follow the instructions on-screen.

For more information, check the [README in the ESP32Firmware folder](ESP32Firmware/README.md).

### Firmware for custom Zephyr-based trackers
If you want to use a Zephyr-supported BLE device (e.g. nRF51/52) as a custom Find My Device tracker, you can find the firmware in the folder ZephyrFirmware. To register a new tracker, run main.py and press 'r' if you are asked to. Afterward, follow the instructions on-screen.

For more information, check the [README in the ZephyrFirmware folder](ZephyrFirmware/README.md).

### iOS App
You can also use my [iOS App](https://testflight.apple.com/join/rGqa2mTe) to access your Find My Device trackers on the go.
