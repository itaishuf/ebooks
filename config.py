from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Production keeps a minimal .env only for Bitwarden bootstrap credentials.
    # Everything else should use config defaults, explicit env overrides, or
    # runtime secrets fetched from Bitwarden at startup.
    model_config = {"env_file": ".env"}

    # Server
    port: int = 19191
    host: str = "0.0.0.0"
    trusted_hosts: list[str] = [
        "localhost",
        "127.0.0.1",
        "::1",
        "testserver",
        "itai-books",
        "*.ts.net",
    ]
    trusted_proxy_ips: list[str] = []

    # Gmail
    gmail_account: str = "itaishuf@gmail.com"
    gmail_password_bw_item_id: str = "d5b23dae-f723-48ac-b1da-6b155b0fbd71"

    # Supabase Auth
    supabase_url: str = "https://cxiroxexywspnysxflru.supabase.co"
    supabase_jwks_url: str = ""
    supabase_issuer: str = ""
    supabase_jwt_audience: str = "authenticated"
    supabase_publishable_key: str = ""
    require_verified_email: bool = True


    # Anna's Archive mirrors (search only, no paid API)
    annas_archive_mirrors: list[str] = [
        "https://annas-archive.org",
        "https://annas-archive.se",
        "https://annas-archive.gs",
        "https://annas-archive.li",
        "https://annas-archive.gl",
        "https://annas-archive.vg",
        "https://annas-archive.pk",
        "https://annas-archive.gd",
    ]
    # Populated at startup with the first healthy mirror
    annas_archive_url: str = ""

    # Paths
    download_dir: str = "/tmp/ebooks"
    log_path: str = "./books.log"
    job_ttl_seconds: int = 6 * 60 * 60
    download_artifact_ttl_seconds: int = 6 * 60 * 60
    cleanup_interval_seconds: int = 5 * 60

    # Abuse controls
    search_rate_limit_per_ip: int = 30
    search_rate_limit_per_user: int = 60
    search_rate_limit_window_seconds: int = 60
    download_rate_limit_per_ip: int = 5
    download_rate_limit_per_user: int = 5
    download_rate_limit_window_seconds: int = 10 * 60
    job_poll_rate_limit_per_ip: int = 120
    job_poll_rate_limit_per_user: int = 240
    job_poll_rate_limit_window_seconds: int = 60
    max_concurrent_download_jobs: int = 5
    max_in_flight_jobs: int = 8
    max_queued_jobs: int = 6
    max_jobs_per_user: int = 5
    max_jobs_per_ip: int = 5
    overload_retry_after_seconds: int = 30

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

    # Bitwarden bootstrap credentials (typically provided by a minimal .env)
    bw_server_url: str = "https://vault.zorilla-iguana.ts.net/"
    bw_client_id: str = ""
    bw_client_secret: str = ""
    bw_master_password: str = ""

    # Runtime application secrets (populated from Bitwarden item IDs at startup)
    gmail_password: str = ""

    # Local development / E2E test configuration
    test_goodreads_url: str = ""
    test_kindle_email: str = ""


settings = Settings()
