import streamlit as st

from config.settings import HARD_DAILY_CAP, get_settings, load_profile

settings = get_settings()

st.title("Settings")
st.caption("Edit `.env` and `config/profile.toml`, then restart the app to apply changes.")

st.subheader("Integrations")
for name, ok in settings.integration_status().items():
    st.markdown(f"{'✅ Configured' if ok else '⚠️ Not configured'} — **{name}**")
st.caption("AI runs on Google Gemini via the google-genai SDK. The Anthropic API is not used.")

st.subheader("Gmail")
st.caption("Send-only permission (`gmail.send`): the app can send emails as you but cannot read your inbox.")
client_ok = settings.gmail_credentials_file.exists()
token_ok = settings.gmail_token_file.exists()
if not client_ok:
    st.info(
        "**To connect Gmail (one-time), in Google Cloud Console:**  \n"
        "1. Search **Gmail API** in the top bar → **Enable**.  \n"
        "2. **Google Auth Platform → Branding**: app name + your email as support email (or **Get started**).  \n"
        "3. **Audience**: External, status Testing → **Test users → Add users** → your Gmail address.  \n"
        "4. **Clients → + Create client** → Application type **Desktop app** → Create → **Download JSON**.  \n"
        f"5. Save the file as `{settings.gmail_credentials_file}`.  \n"
        "6. Set `SENDER_EMAIL` / `SENDER_NAME` in `.env`, restart, then click **Connect Gmail** here.  \n"
        "_(Older consoles call step 4 “Credentials → Create credentials → OAuth client ID”.)_"
    )
else:
    g1, g2 = st.columns(2)
    g1.write("✅ Connected" if token_ok else "⚠️ Not connected yet")
    if g1.button("🔗 Connect Gmail" if not token_ok else "🔁 Reconnect Gmail",
                 help="Opens Google's sign-in page in your browser on this computer."):
        from integrations.gmail import authorize

        try:
            with st.spinner("Waiting for you to approve access in the browser tab that opened…"):
                authorize(settings.gmail_credentials_file, settings.gmail_token_file)
            st.success("Gmail connected.")
            st.rerun()
        except Exception as exc:
            st.error(f"Could not connect Gmail: {exc}")
    if token_ok:
        from integrations.gmail import METADATA_SCOPE

        tracking = METADATA_SCOPE in settings.gmail_token_file.read_text(encoding="utf-8")
        g1.write("✅ Reply tracking on" if tracking else "Reply tracking off (mark replies manually)")
        if not tracking and g1.button("Enable reply tracking", help="Reconnects Gmail with read access to email "
                                      "headers only (From/Subject/date), never message content."):
            from integrations.gmail import authorize

            try:
                authorize(settings.gmail_credentials_file, settings.gmail_token_file, reply_tracking=True)
                st.rerun()
            except Exception as exc:
                st.error(f"Could not enable reply tracking: {exc}")
    if token_ok and g2.button("Disconnect Gmail", help="Deletes the stored token on this computer."):
        settings.gmail_token_file.unlink(missing_ok=True)
        st.rerun()
if not settings.sender_email:
    st.caption("Tip: set `SENDER_EMAIL` (and `SENDER_NAME`) in `.env` so the From line and test emails use them.")

st.subheader("Sending rules")
c1, c2, c3 = st.columns(3)
c1.metric("Daily limit", settings.daily_email_limit, help=f"Hard cap is {HARD_DAILY_CAP}.")
c2.metric("India / international", f"{settings.india_ratio:.0%} / {1 - settings.india_ratio:.0%}")
c3.metric("Mode", "Hybrid approval")
st.caption(f"Batch sends pause about {settings.send_interval_seconds:g} s between emails "
           "(`SEND_INTERVAL_SECONDS`).")
st.write(f"Gemini model: `{settings.gemini_model}` · fallback: `{settings.gemini_fallback_model or 'none'}` "
         f"· Database: `{settings.database_path}`")

st.subheader("Applicant profile")
try:
    profile = load_profile()
    st.json(profile)
except FileNotFoundError:
    st.error("config/profile.toml is missing.")
