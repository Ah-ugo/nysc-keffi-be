from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "nysc_keffi"
    jwt_secret: str = "dev-only-change-me"
    jwt_minutes: int = 480
    cors_origins: str = "http://localhost:5173"
    cloudinary_cloud_name: str = ""
    cloudinary_api_key: str = ""
    cloudinary_api_secret: str = ""
    face_provider: str = "opencv"
    aws_region: str = "eu-west-1"
    face_match_threshold: float = 90.0
    face_review_threshold: float = 75.0
    local_face_match_threshold: float = 0.363
    camp_lat: float = 0.0
    camp_lon: float = 0.0
    camp_radius_m: float = 500
    max_gps_accuracy_m: float = 100
    callup_regex: str = r"^[A-Z0-9/\-]{6,25}$"
    selfie_retention_days: int = 30
    liveness_mode: str = "none"  # none | pose_challenge
    liveness_pair_threshold: float = 70.0
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = "mailto:admin@example.com"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "noreply@example.com"
    director_username: str = "director"
    director_password: str = "ChangeMe!2026"


settings = Settings()
