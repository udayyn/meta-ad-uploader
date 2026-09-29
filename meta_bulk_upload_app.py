"""
meta_bulk_upload_app.py
------------------------
The simple way to make Meta ads: point to an ad you already like (a "reference
ad"), and this app copies its text, link, and settings automatically — you
just swap in a new image or video. Works for one ad, or many at once from a
sheet of Google Drive links.

HOW TO RUN:
    pip install streamlit requests pandas
    streamlit run meta_bulk_upload_app.py
"""

import os
import re
import time
import mimetypes

import requests
import streamlit as st
import pandas as pd

GRAPH_API_VERSION = "v21.0"
BASE_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
VIDEO_EXTENSIONS = (".mp4", ".mov", ".avi", ".mkv", ".m4v")

st.set_page_config(page_title="Meta Ad Uploader", layout="wide")

# --------------------------------------------------------------------------
# Simple password gate — set APP_PASSWORD in Streamlit Cloud's Secrets panel.
# --------------------------------------------------------------------------
def check_password():
    def password_entered():
        if st.session_state.get("password") == st.secrets.get("APP_PASSWORD", ""):
            st.session_state["password_correct"] = True
            del st.session_state["password"]
        else:
            st.session_state["password_correct"] = False

    if st.session_state.get("password_correct"):
        return True

    st.text_input("Password", type="password", on_change=password_entered, key="password")
    if "password_correct" in st.session_state and not st.session_state["password_correct"]:
        st.error("Incorrect password.")
    return False


if not check_password():
    st.stop()

# --------------------------------------------------------------------------
# Sidebar — credentials, entered once per session (never saved to disk)
# --------------------------------------------------------------------------
st.sidebar.header("Meta credentials")
access_token = st.sidebar.text_input("Access Token", type="password")
ad_account_id = st.sidebar.text_input("Ad Account ID", placeholder="act_1234567890")
st.sidebar.caption("These stay in this browser session only — nothing is saved to a file.")


def creds_ready():
    return bool(access_token and ad_account_id)


# --------------------------------------------------------------------------
# Meta Graph API calls
# --------------------------------------------------------------------------
def upload_image(file_bytes: bytes, filename: str) -> str:
    url = f"{BASE_URL}/{ad_account_id}/adimages"
    resp = requests.post(url, data={"access_token": access_token}, files={filename: file_bytes})
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(f"Image upload failed: {data}")
    images = data.get("images", {})
    first_key = next(iter(images))
    return images[first_key]["hash"]


def upload_video(file_bytes: bytes, filename: str) -> str:
    url = f"{BASE_URL}/{ad_account_id}/advideos"
    mime_type = mimetypes.guess_type(filename)[0] or "video/mp4"
    resp = requests.post(
        url,
        data={"access_token": access_token},
        files={"source": (filename, file_bytes, mime_type)},
    )
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(f"Video upload failed: {data}")
    video_id = data["id"]

    status_url = f"{BASE_URL}/{video_id}"
    for _ in range(30):
        status_resp = requests.get(status_url, params={
            "fields": "status", "access_token": access_token,
        }).json()
        video_status = status_resp.get("status", {}).get("video_status")
        if video_status == "ready":
            return video_id
        if video_status == "error":
            raise RuntimeError(f"Video processing failed: {status_resp}")
        time.sleep(10)
    raise RuntimeError("Timed out waiting for video to finish processing.")


def get_reference_ad_details(reference_ad_id):
    """Reads an existing ad's text, link, CTA, page, and ad set — so a new ad can reuse them."""
    url = f"{BASE_URL}/{reference_ad_id}"
    resp = requests.get(url, params={
        "fields": "adset_id,creative{object_story_spec}",
        "access_token": access_token,
    })
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(f"Could not read reference ad {reference_ad_id}: {data}")

    oss = data.get("creative", {}).get("object_story_spec", {})
    link_data = oss.get("link_data", {})
    video_data = oss.get("video_data", {})
    cta = link_data.get("call_to_action") or video_data.get("call_to_action") or {}

    return {
        "adset_id": data.get("adset_id"),
        "page_id": oss.get("page_id"),
        "message": link_data.get("message") or video_data.get("message") or "",
        "headline": link_data.get("name") or video_data.get("title") or "",
        "description": link_data.get("description") or video_data.get("link_description") or "",
        "cta_type": cta.get("type", "LEARN_MORE"),
        "link_url": link_data.get("link") or cta.get("value", {}).get("link", ""),
    }


def create_creative_from_reference(ad_name, ref, image_hash=None, video_id=None, overrides=None) -> str:
    """Builds a new creative that reuses the reference ad's text/link/CTA, with a new image or
    video. `overrides` (optional dict) can replace any of message/headline/description/link_url/cta_type —
    anything not given falls back to the reference ad's own value."""
    overrides = overrides or {}
    message = overrides.get("message") or ref["message"]
    headline = overrides.get("headline") or ref["headline"]
    description = overrides.get("description") or ref["description"]
    link_url = overrides.get("link_url") or ref["link_url"]
    cta_type = overrides.get("cta_type") or ref["cta_type"]

    url = f"{BASE_URL}/{ad_account_id}/adcreatives"
    object_story_spec = {"page_id": ref["page_id"]}

    if video_id:
        object_story_spec["video_data"] = {
            "video_id": video_id,
            "message": message,
            "title": headline,
            "link_description": description,
            "call_to_action": {"type": cta_type, "value": {"link": link_url}},
        }
    else:
        object_story_spec["link_data"] = {
            "message": message,
            "link": link_url,
            "name": headline,
            "description": description,
            "image_hash": image_hash,
            "call_to_action": {"type": cta_type, "value": {"link": link_url}},
        }

    payload = {
        "name": f"{ad_name}_creative",
        "object_story_spec": object_story_spec,
        "access_token": access_token,
    }
    resp = requests.post(url, json=payload)
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(f"Creative creation failed: {data}")
    return data["id"]


def create_ad(ad_name, ad_set_id, creative_id, status) -> str:
    url = f"{BASE_URL}/{ad_account_id}/ads"
    payload = {
        "name": ad_name,
        "adset_id": ad_set_id,
        "creative": {"creative_id": creative_id},
        "status": status,
        "access_token": access_token,
    }
    resp = requests.post(url, json=payload)
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(f"Ad creation failed: {data}")
    return data["id"]


# --------------------------------------------------------------------------
# Google Drive helper — download a file directly from a share link
# --------------------------------------------------------------------------
def get_instagram_actor_id(page_id):
    """Looks up the Instagram Business account connected to a Facebook Page."""
    url = f"{BASE_URL}/{page_id}"
    resp = requests.get(url, params={"fields": "instagram_business_account,name", "access_token": access_token})
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(f"Could not look up this Page: {data}")
    ig = data.get("instagram_business_account")
    if not ig:
        raise RuntimeError(f"'{data.get('name', page_id)}' doesn't have an Instagram Business account connected to it.")
    return ig["id"]


def extract_drive_file_id(url):
    for pattern in [r"/file/d/([a-zA-Z0-9_-]+)", r"[?&]id=([a-zA-Z0-9_-]+)", r"/d/([a-zA-Z0-9_-]+)"]:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def download_drive_file(url):
    """Downloads a file from a Google Drive share link. The file must be shared as
    'Anyone with the link' can view, or this app can't reach it."""
    file_id = extract_drive_file_id(url.strip())
    if not file_id:
        raise RuntimeError(f"Couldn't find a Google Drive file ID in this link: {url}")

    session = requests.Session()
    download_url = "https://drive.google.com/uc?export=download"
    resp = session.get(download_url, params={"id": file_id}, stream=True)

    token = next((v for k, v in resp.cookies.items() if k.startswith("download_warning")), None)
    if token:
        resp = session.get(download_url, params={"id": file_id, "confirm": token}, stream=True)

    content = resp.content
    content_type = resp.headers.get("content-type", "")
    if content_type.startswith("text/html"):
        raise RuntimeError(
            f"Got a webpage instead of a file from this Drive link — make sure it's shared as "
            f"'Anyone with the link' can view: {url}"
        )

    filename = file_id
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r'filename="?([^";]+)"?', cd)
    if m:
        filename = m.group(1)
    elif "." not in filename:
        filename += ".mp4" if content_type.startswith("video") else ".jpg"

    return content, filename, content_type


def is_video_file(filename, content_type=""):
    return filename.lower().endswith(VIDEO_EXTENSIONS) or content_type.startswith("video")


def create_partnership_ad_creative(ad_name, instagram_actor_id, instagram_media_id,
                                    cta_type=None, link_url=None) -> str:
    """Creates a creative from an existing Instagram post via Partnership Ads. Requires that
    permission for this post was already granted through Ads Manager's Partnership Ads Hub
    (redeeming the creator's ad code) — this app can't do that redemption step itself."""
    url = f"{BASE_URL}/{ad_account_id}/adcreatives"
    object_story_spec = {
        "instagram_actor_id": instagram_actor_id,
        "source_instagram_media_id": instagram_media_id,
    }
    if cta_type:
        cta = {"type": cta_type}
        if link_url:
            cta["value"] = {"link": link_url}
        object_story_spec["call_to_action"] = cta

    payload = {
        "name": f"{ad_name}_creative",
        "object_story_spec": object_story_spec,
        "access_token": access_token,
    }
    resp = requests.post(url, json=payload)
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(f"Partnership ad creative creation failed: {data}")
    return data["id"]


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
st.title("Meta Ad Uploader")
st.caption("Point to an ad you like, swap in a new image or video — that's it.")

tab1, tab2, tab3 = st.tabs(["Single ad", "Bulk from sheet", "Partnership ad"])

# ---- Tab 1: single ad ---------------------------------------------------
with tab1:
    st.subheader("Create one ad from a reference")
    with st.form("single_ad_form"):
        reference_ad_id = st.text_input(
            "Reference Ad ID",
            help="Paste the Ad ID of an existing ad whose text, link, and settings you want to reuse.",
        )
        ad_set_id_override = st.text_input(
            "Ad Set ID (optional)",
            help="Leave blank to put the new ad in the same ad set as the reference ad.",
        )
        ad_name = st.text_input("New ad name")
        creative_file = st.file_uploader("Upload the new image or video", type=["jpg", "jpeg", "png", "mp4", "mov"])
        status = st.radio("Status", ["PAUSED", "ACTIVE"], horizontal=True,
                           help="Leave as PAUSED to review in Ads Manager before it spends money.")

        with st.expander("Change the ad copy too (optional — leave blank to keep the reference ad's copy)"):
            primary_text_override = st.text_area("Primary text")
            headline_override = st.text_input("Headline")
            description_override = st.text_input("Description")
            link_url_override = st.text_input("Link URL")
            cta_override = st.selectbox("Call to action", [
                "(keep reference ad's)", "LEARN_MORE", "SHOP_NOW", "SIGN_UP",
                "BOOK_TRAVEL", "DOWNLOAD", "GET_QUOTE",
            ])

        submitted = st.form_submit_button("Create ad")

    if submitted:
        if not creds_ready():
            st.error("Fill in your Access Token and Ad Account ID in the sidebar first.")
        elif not all([reference_ad_id, ad_name, creative_file]):
            st.error("Fill in the Reference Ad ID, ad name, and upload a creative file.")
        else:
            try:
                with st.spinner("Reading the reference ad, uploading your creative, and building the new ad..."):
                    ref = get_reference_ad_details(reference_ad_id)
                    target_adset_id = ad_set_id_override.strip() or ref["adset_id"]

                    overrides = {
                        "message": primary_text_override.strip(),
                        "headline": headline_override.strip(),
                        "description": description_override.strip(),
                        "link_url": link_url_override.strip(),
                        "cta_type": None if cta_override == "(keep reference ad's)" else cta_override,
                    }

                    file_bytes = creative_file.getvalue()
                    if is_video_file(creative_file.name):
                        video_id = upload_video(file_bytes, creative_file.name)
                        creative_id = create_creative_from_reference(ad_name, ref, video_id=video_id, overrides=overrides)
                    else:
                        image_hash = upload_image(file_bytes, creative_file.name)
                        creative_id = create_creative_from_reference(ad_name, ref, image_hash=image_hash, overrides=overrides)

                    ad_id = create_ad(ad_name, target_adset_id, creative_id, status)
                st.success(f"Created ad '{ad_name}' — ID: {ad_id} (status: {status})")
            except Exception as e:
                st.error(f"Failed: {e}")

# ---- Tab 2: bulk from sheet ----------------------------------------------
with tab2:
    st.subheader("Create many ads at once, straight from a sheet")
    st.caption(
        "One row = one ad. Paste a Google Drive link to the new image or video for each — "
        "images and videos can be mixed freely in the same sheet. Make sure every Drive file "
        "is shared as 'Anyone with the link' can view. The copy columns are optional — leave "
        "any of them blank to keep that reference ad's original text."
    )

    template_df = pd.DataFrame([{
        "reference_ad_id": "120212345678900123",
        "ad_set_id": "",
        "ad_name": "NH_Serum_CreativeA",
        "creative_drive_link": "https://drive.google.com/file/d/XXXXXXXX/view",
        "primary_text": "",
        "headline": "",
        "description": "",
        "link_url": "",
        "call_to_action": "",
        "status": "PAUSED",
    }])
    st.download_button("Download sheet template", template_df.to_csv(index=False),
                        file_name="bulk_ads_template.csv", mime="text/csv")

    csv_file = st.file_uploader("Upload your filled-in sheet (CSV)", type="csv", key="csv_uploader")
    dry_run = st.checkbox("Dry run (check the sheet without creating anything on Meta)", value=True)
    run_bulk = st.button("Process rows")

    REQUIRED_COLS = ["reference_ad_id", "ad_name", "creative_drive_link"]

    if run_bulk:
        if not csv_file:
            st.error("Upload a sheet first.")
        elif not dry_run and not creds_ready():
            st.error("Fill in your Access Token and Ad Account ID in the sidebar first.")
        else:
            try:
                df = pd.read_csv(csv_file, dtype=str).fillna("")
                df.columns = [c.strip() for c in df.columns]
            except Exception as e:
                st.error(f"Could not read that CSV file: {e}")
                st.stop()

            for col in df.columns:
                df[col] = df[col].astype(str).str.strip()

            with st.expander("Debug info — what the app is seeing", expanded=False):
                st.write("Columns found:", list(df.columns))
                st.dataframe(df, use_container_width=True)

            missing_cols = [c for c in REQUIRED_COLS if c not in df.columns]
            if missing_cols:
                st.error(f"Your sheet is missing these required columns: {missing_cols}. "
                          f"Check the exact spelling against the template.")
                st.stop()

            results = []

            for i, row in df.iterrows():
                row_label = row.get("ad_name") or f"row {i+2}"
                missing_required = [c for c in REQUIRED_COLS if not row.get(c)]
                if missing_required:
                    results.append({"ad": row_label, "result": f"SKIPPED — missing: {missing_required}"})
                    continue

                if dry_run:
                    results.append({"ad": row_label, "result": "OK (dry run)"})
                    continue

                try:
                    status = (row.get("status") or "PAUSED").upper()
                    ref = get_reference_ad_details(row["reference_ad_id"])
                    target_adset_id = row.get("ad_set_id", "").strip() or ref["adset_id"]

                    overrides = {
                        "message": row.get("primary_text", ""),
                        "headline": row.get("headline", ""),
                        "description": row.get("description", ""),
                        "link_url": row.get("link_url", ""),
                        "cta_type": row.get("call_to_action", "").upper() or None,
                    }

                    file_bytes, filename, content_type = download_drive_file(row["creative_drive_link"])

                    if is_video_file(filename, content_type):
                        video_id = upload_video(file_bytes, filename)
                        creative_id = create_creative_from_reference(row["ad_name"], ref, video_id=video_id, overrides=overrides)
                    else:
                        image_hash = upload_image(file_bytes, filename)
                        creative_id = create_creative_from_reference(row["ad_name"], ref, image_hash=image_hash, overrides=overrides)

                    ad_id = create_ad(row["ad_name"], target_adset_id, creative_id, status)
                    results.append({"ad": row_label, "result": f"Created — ID {ad_id} ({status})"})
                except Exception as e:
                    results.append({"ad": row_label, "result": f"FAILED — {e}"})

            results_df = pd.DataFrame(results)
            st.dataframe(results_df, use_container_width=True)

            fail_count = results_df["result"].str.contains("FAILED|SKIPPED", na=False).sum() if not results_df.empty else 0
            ok_count = len(results_df) - fail_count
            st.caption(f"{ok_count} succeeded, {fail_count} had issues — check the 'result' column above for exact reasons.")

# ---- Tab 3: partnership ad ------------------------------------------------
with tab3:
    st.subheader("Create an ad from a creator's Instagram post")
    st.info(
        "This needs the creator's partnership permission set up once already, through Ads "
        "Manager's **Partnership Ads Hub** (where you redeem their ad code) — that's a manual, "
        "one-time click-through on Meta's site with no API shortcut. Once that's done, Ads "
        "Manager will show you the post's **Instagram Media ID**, which is what this app needs "
        "below. A plain Instagram post link isn't enough on its own."
    )

    st.markdown("**Step 1 — Look up your Instagram Actor ID** (this part *is* automatic)")
    lookup_page_id = st.text_input("Your Page ID", key="pa_lookup_page_id")
    if st.button("Look up Instagram Actor ID"):
        if not creds_ready():
            st.error("Fill in your Access Token and Ad Account ID in the sidebar first.")
        elif not lookup_page_id:
            st.error("Enter your Page ID first.")
        else:
            try:
                found_id = get_instagram_actor_id(lookup_page_id.strip())
                st.session_state["pa_actor_id_found"] = found_id
                st.success(f"Found it: {found_id} (filled in below)")
            except Exception as e:
                st.error(f"Couldn't look it up: {e}")

    st.markdown("**Step 2 — Fill in the rest**")
    with st.form("partnership_ad_form"):
        pa_ad_set_id = st.text_input("Ad Set ID")
        pa_ad_name = st.text_input("Ad name")
        pa_instagram_actor_id = st.text_input(
            "Your Instagram Actor ID",
            value=st.session_state.get("pa_actor_id_found", ""),
            help="Filled in automatically after Step 1, or paste it yourself if you already have it.",
        )
        pa_media_id = st.text_input(
            "Instagram Media ID (from Partnership Ads Hub)",
            help="The numeric ID Ads Manager gives you for the authorized post, after redeeming "
                 "the partner's code in Partnership Ads Hub. Not a post link.",
        )
        pa_status = st.radio("Status", ["PAUSED", "ACTIVE"], horizontal=True, key="pa_status")

        with st.expander("Add a link/call-to-action button (optional)"):
            pa_cta = st.selectbox("Call to action", [
                "(none)", "LEARN_MORE", "SHOP_NOW", "SIGN_UP", "BOOK_TRAVEL", "DOWNLOAD", "GET_QUOTE",
            ])
            pa_link_url = st.text_input("Link URL", key="pa_link")

        pa_submitted = st.form_submit_button("Create partnership ad")

    if pa_submitted:
        if not creds_ready():
            st.error("Fill in your Access Token and Ad Account ID in the sidebar first.")
        elif not all([pa_ad_set_id, pa_ad_name, pa_instagram_actor_id, pa_media_id]):
            st.error("Fill in the Ad Set ID, ad name, Instagram Actor ID, and Media ID.")
        else:
            try:
                with st.spinner("Creating the partnership ad..."):
                    cta_type = None if pa_cta == "(none)" else pa_cta
                    creative_id = create_partnership_ad_creative(
                        pa_ad_name, pa_instagram_actor_id, pa_media_id.strip(),
                        cta_type=cta_type, link_url=pa_link_url.strip() or None,
                    )
                    ad_id = create_ad(pa_ad_name, pa_ad_set_id, creative_id, pa_status)
                st.success(f"Created partnership ad '{pa_ad_name}' — ID: {ad_id} (status: {pa_status})")
            except Exception as e:
                st.error(f"Failed: {e}")
