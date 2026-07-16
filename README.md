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

Open PowerShell and run:

```powershell
& ([scriptblock]::Create((irm https://jcardus.github.io/GoogleFindMyTools/setup.ps1)))
```

On macOS or Linux:

Open Terminal and run:

```bash
curl -fsSL https://jcardus.github.io/GoogleFindMyTools/setup.sh | bash
```

When prompted for `PROVISIONING_TOKEN`, paste the token from the Tagora hub owner. The script removes the local auth cache, opens Chrome for Google login, asks for any required Find Hub E2EE approval, then uploads the fresh cache to the Tagora backend.
