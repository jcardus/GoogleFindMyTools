# GoogleFindMyTools

This repository includes some useful tools that reimplement parts of Google's Find My Device Network (now called Find Hub Network). Note that the code of this repo is still very experimental.

### What's possible?
Currently, it is possible to query Find My Device / Find Hub trackers and Android devices, read out their E2EE keys, and decrypt encrypted locations sent from the Find My Device / Find Hub network. You can also send register your own ESP32- or Zephyr-based trackers, as described below.

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

### Authentication

On the first run, an authentication sequence is executed, which requires a computer with access to Google Chrome.

The authentication results are stored in `Auth/secrets.json`. If you intend to run this tool on a headless machine, you can just copy this file to avoid having to use Chrome.

To create or refresh the auth cache directly, run `python provision_account_auth.py`. For hub deployments that should also upload the cache to R2 and upsert Supabase, run `python provision_google_account.py --run-auth`.

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
