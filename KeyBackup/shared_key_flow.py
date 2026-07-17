#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

import json
import time

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as ec

from KeyBackup.response_parser import get_fmdn_shared_key
from KeyBackup.shared_key_request import get_security_domain_request_url
from chrome_driver import create_driver, safe_quit_driver

def request_shared_key_flow(driver=None):
    owns_driver = driver is None
    if owns_driver:
        driver = create_driver()
    try:
        # The unlock endpoint rejects an unauthenticated initial request. Send
        # the user through one explicit login that returns to My Account, then
        # continue to PIN approval in this same browser session.
        login_url = (
            "https://accounts.google.com/ServiceLogin"
            "?continue=https%3A%2F%2Fmyaccount.google.com%2F"
        )
        driver.get(login_url)
        try:
            WebDriverWait(driver, 300).until(
                lambda current: current.current_url.startswith("https://myaccount.google.com/")
            )
        except TimeoutException as exc:
            raise TimeoutError("Timed out waiting for Google sign-in (5 minutes).") from exc
        print("[SharedKeyFlow] Signed in successfully; requesting encrypted-key approval.")

        # Install the Android bridge before navigating so the unlock page can
        # call it as soon as it loads, including after PIN redirects.
        script = """
        window.mm = {
            setVaultSharedKeys: function(str, vaultKeys) {
                console.log('setVaultSharedKeys called with:', str, vaultKeys);
                alert(JSON.stringify({ method: 'setVaultSharedKeys', str: str, vaultKeys: vaultKeys }));
            },
            closeView: function() {
                console.log('closeView called');
                alert(JSON.stringify({ method: 'closeView' }));
            }
        };
        """
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": script},
        )

        # The authenticated unlock URL now only needs encrypted-key PIN approval.
        security_url = get_security_domain_request_url()
        driver.get(security_url)

        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            # Check for alerts indicating JavaScript calls
            try:
                WebDriverWait(driver, 1).until(ec.alert_is_present())
            except TimeoutException:
                continue

            alert = driver.switch_to.alert
            message = alert.text
            alert.accept()
            data = json.loads(message)

            if data['method'] == 'setVaultSharedKeys':
                shared_key = get_fmdn_shared_key(data['vaultKeys'])
                print("[SharedKeyFlow] Received Shared Key.")
                return shared_key.hex()
            if data['method'] == 'closeView':
                raise RuntimeError("Google closed encrypted-key approval without returning a shared key.")

        raise TimeoutError("Timed out waiting for Google encrypted-key approval (5 minutes).")
    finally:
        if owns_driver:
            safe_quit_driver(driver)


if __name__ == "__main__":
   request_shared_key_flow()
