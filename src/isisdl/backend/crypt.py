import base64
import getpass
import os
from typing import Optional, cast

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from isisdl.backend.utils import User, config, error_text, path
from isisdl.settings import password_hash_algorithm, password_hash_length, password_hash_iterations, env_var_name_username, env_var_name_password, is_autorun, master_password, database_file_location


def generate_key(password: str) -> bytes:
    # You might notice that the salt is empty. This is a deliberate decision.
    # Since the salt file only protects against brute forcing of multiple passwords
    # and the password file is limited to a single system (no central storage) it doesn't make sense to include it.
    salt = b""

    kdf = PBKDF2HMAC(algorithm=password_hash_algorithm(), length=password_hash_length, salt=salt, iterations=password_hash_iterations)

    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def encryptor(password: str, content: str) -> str:
    key = generate_key(password)
    return Fernet(key).encrypt(content.encode()).decode()


def decryptor(password: str, content: str) -> Optional[str]:
    key = generate_key(password)

    try:
        return Fernet(key).decrypt(content.encode()).decode()
    except InvalidToken:
        return None


def store_user(user: User, password: Optional[str] = None) -> None:
    encrypted = encryptor(password or master_password, user.password)

    config["username"] = user.username
    config["password_encrypted"] = bool(password)
    config["password"] = encrypted


def get_credentials() -> User:
    """
    Prioritizes:
        Environment variable > Database > Input
    """

    # First check the environment variables for username *and* password
    username, password = os.getenv(env_var_name_username), os.getenv(env_var_name_password)
    if username is not None and password is not None:
        return User(username, password)

    # Now query the database
    username = cast(Optional[str], config["username"])
    password = cast(Optional[str], config["password"])
    user_store_encrypted = cast(bool, config["password_encrypted"])

    if username is not None and password is not None:
        if not user_store_encrypted:
            decrypted = decryptor(master_password, password)
            if decrypted is None:
                print(f"{error_text}\nI could not decrypt the password even though it is set to be encrypted with the master password.\n"
                      f"You can delete the file `{path(database_file_location)}` and store your password again!")
                os._exit(1)

            return User(username, decrypted)

        while True:
            user_password = getpass.getpass("Please enter the passphrase: ")
            actual_password = decryptor(user_password, password)
            if actual_password is None:
                print("Your password is incorrect. Try again\n")
            else:
                return User(username, actual_password)

    if is_autorun:
        exit(1)

    # If nothing is found prompt the user
    print("Please provide authentication for ISIS.")
    username = input("Username: ")
    password = getpass.getpass("Password: ")

    return User(username, password)
