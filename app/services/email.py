import logging

from app.config import settings

logger = logging.getLogger(__name__)


def send_verification_email(email: str, token: str) -> None:
    verification_url = f"{settings.FRONTEND_URL}/verify-email?token={token}"

    logger.info("Verification URL for %s: %s", email, verification_url)

    if not settings.RESEND_API_KEY:
        return

    import resend

    resend.api_key = settings.RESEND_API_KEY
    resend.Emails.send(
        {
            "from": settings.RESEND_FROM_EMAIL,
            "to": email,
            "subject": "Verify your BackKitchen email address",
            "html": f"""
<!DOCTYPE html>
<html>
<body style="background:#111111;font-family:sans-serif;color:#ffffff;padding:40px 0;">
  <div style="max-width:480px;margin:0 auto;background:#1A1A1A;border:1px solid #2E2E2E;padding:40px;">
    <div style="margin-bottom:24px;">
      <span style="font-family:monospace;font-size:24px;font-weight:700;color:#ffffff;">BackKitchen</span>
    </div>
    <h2 style="font-family:monospace;font-size:18px;font-weight:600;color:#ffffff;margin:0 0 12px;">
      Verify your email address
    </h2>
    <p style="color:#B8B9B6;font-size:14px;line-height:1.6;margin:0 0 24px;">
      Thanks for signing up. Click the button below to verify your email address. The link expires in 30 minutes.
    </p>
    <a href="{verification_url}"
       style="display:inline-block;background:#FF8400;color:#111111;font-family:monospace;font-size:14px;
              font-weight:500;padding:10px 24px;border-radius:9999px;text-decoration:none;">
      Verify Email
    </a>
    <p style="color:#B8B9B6;font-size:12px;margin:24px 0 0;">
      If you did not create an account, you can ignore this email.
    </p>
  </div>
</body>
</html>
""",
        }
    )
