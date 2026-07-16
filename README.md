# Connect your Google account to Tagora

Follow these steps to connect a Google account to a Tagora hub.

### How to use

> [!CAUTION]
> Before starting, ensure you have Chrome updated and Python 3.12 installed. Other Python versions are not supported for this repo.
> 
> **If Chrome is not up to date, the script will NOT work, guaranteed!**

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
