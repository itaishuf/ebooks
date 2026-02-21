from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env"}

    # Server
    port: int = 19191
    host: str = "0.0.0.0"

    # Gmail
    gmail_account: str = "itaishuf@gmail.com"
    gmail_password_bw_item_title: str = "Ebookarr"

    # Anna's Archive
    annas_archive_api_key_bw_item_title: str = "annas-archive.org (8pz82Gt)"

    # Paths
    download_dir: str = "/tmp/ebooks"
    log_path: str = "./books.log"

    # Libgen mirrors
    libgen_mirrors: list[str] = [
        "https://libgen.is",
        "https://libgen.st",
        "https://libgen.bz",
        "https://libgen.gs",
        "https://libgen.la",
        "https://libgen.gl",
        "https://libgen.li",
        "https://libgen.rs",
    ]

    # Selenium
    selenium_download_timeout_minutes: int = 10
    selenium_click_attempts: int = 3

    # Bitwarden credentials (loaded from .env)
    bw_client_id: str = ""
    bw_client_secret: str = ""
    bw_master_password: str = ""

    # Bitwarden item titles
    api_key_bw_item_title: str = "Ebookarr API Key"

    # Runtime secrets (populated from Bitwarden at startup)
    gmail_password: str = ""
    annas_archive_api_key: str = ""
    api_key: str = ""


settings = Settings()
