"""
meta_bulk_upload_app.py
------------------------
A browser-based (Streamlit) tool for uploading image/video creatives to Meta
and creating ads from them — no command-line editing needed after setup.

HOW TO RUN (see the setup guide for full beginner steps):
    pip install streamlit requests
    streamlit run meta_bulk_upload_app.py

This opens a page in your browser where you can:
  - Enter your Meta Access Token / Ad Account ID / Page ID once per session
  - Create a single ad by filling in a form and uploading one image/video
  - Or upload a CSV + a batch of creative files to create many ads at once
"""

import os
import re
import time
import mimetypes
import tempfile

import requests
import streamlit as st
import pandas as pd

GRAPH_API_VERSION = "v21.0"

st.set_page_config(page_title="Meta Bulk Ad Upload", layout="wide")

# --------------------------------------------------------------------------
# Simple password gate — set APP_PASSWORD in Streamlit Cloud's Secrets panel
# (App settings -> Secrets). Nothing below this renders until it's correct.
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
page_id = st.sidebar.text_input("Page ID", placeholder="1234567890")
st.sidebar.caption("These stay in this browser session only — nothing is saved to a file.")

BASE_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


def creds_ready():
    return bool(access_token and ad_account_id and page_id)


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


def create_ad_creative(ad_name, primary_text, headline, description, link_url, cta,
                        image_hash=None, video_id=None) -> str:
    url = f"{BASE_URL}/{ad_account_id}/adcreatives"

    object_story_spec = {"page_id": page_id}
    if video_id:
        object_story_spec["video_data"] = {
            "video_id": video_id,
            "message": primary_text,
            "title": headline,
            "link_description": description,
            "call_to_action": {"type": cta, "value": {"link": link_url}},
        }
    else:
        object_story_spec["link_data"] = {
            "message": primary_text,
            "link": link_url,
            "name": headline,
            "description": description,
            "image_hash": image_hash,
            "call_to_action": {"type": cta, "value": {"link": link_url}},
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


def create_carousel_creative(ad_name, primary_text, see_more_link, cta, cards) -> str:
    """cards: list of dicts with keys link, headline, description, image_hash (or video_id)"""
    url = f"{BASE_URL}/{ad_account_id}/adcreatives"

    child_attachments = []
    for card in cards:
        attachment = {
            "link": card["link"],
            "name": card["headline"],
            "description": card.get("description", ""),
        }
        if card.get("video_id"):
            attachment["video_id"] = card["video_id"]
        else:
            attachment["image_hash"] = card["image_hash"]
        child_attachments.append(attachment)

    object_story_spec = {
        "page_id": page_id,
        "link_data": {
            "message": primary_text,
            "link": see_more_link,
            "call_to_action": {"type": cta, "value": {"link": see_more_link}},
            "child_attachments": child_attachments,
        },
    }

    payload = {
        "name": f"{ad_name}_creative",
        "object_story_spec": object_story_spec,
        "access_token": access_token,
    }
    resp = requests.post(url, json=payload)
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(f"Carousel creative creation failed: {data}")
    return data["id"]


def create_collection_creative(ad_name, primary_text, headline, link_url, cta, product_set_id,
                                cover_image_hash=None, cover_video_id=None) -> str:
    """Collection ads need an existing Catalog + Product Set (from Commerce Manager) —
    product_set_id below must reference one you already have. If Meta rejects the request,
    the error message it returns usually says exactly which field/permission is the problem."""
    url = f"{BASE_URL}/{ad_account_id}/adcreatives"

    template_data = {
        "link": link_url,
        "message": primary_text,
        "name": headline,
        "call_to_action": {"type": cta, "value": {"link": link_url}},
    }
    if cover_video_id:
        template_data["video_data"] = {"video_id": cover_video_id}
    elif cover_image_hash:
        template_data["image_hash"] = cover_image_hash

    payload = {
        "name": f"{ad_name}_creative",
        "object_story_spec": {
            "page_id": page_id,
            "template_data": template_data,
        },
        "product_set_id": product_set_id,
        "access_token": access_token,
    }
    resp = requests.post(url, json=payload)
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(f"Collection creative creation failed: {data}")
    return data["id"]


def update_ad_set_budget(ad_set_id, daily_budget):
    url = f"{BASE_URL}/{ad_set_id}"
    resp = requests.post(url, data={"daily_budget": daily_budget, "access_token": access_token})
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(f"Ad set budget update failed: {data}")


# --------------------------------------------------------------------------
# Google Drive helpers — download a file directly from a share link
# --------------------------------------------------------------------------
def extract_drive_file_id(url):
    for pattern in [r"/file/d/([a-zA-Z0-9_-]+)", r"[?&]id=([a-zA-Z0-9_-]+)", r"/d/([a-zA-Z0-9_-]+)"]:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def download_drive_file(url):
    """Downloads a file from a Google Drive share link. The file must be shared as
    'Anyone with the link' can view, or Meta's servers (and this app) can't reach it."""
    file_id = extract_drive_file_id(url.strip())
    if not file_id:
        raise RuntimeError(f"Couldn't find a Google Drive file ID in this link: {url}")

    session = requests.Session()
    download_url = "https://drive.google.com/uc?export=download"
    resp = session.get(download_url, params={"id": file_id}, stream=True)

    # Large files trigger a "can't scan for viruses" confirmation page — handle it.
    token = next((v for k, v in resp.cookies.items() if k.startswith("download_warning")), None)
    if token:
        resp = session.get(download_url, params={"id": file_id, "confirm": token}, stream=True)

    content = resp.content
    if resp.headers.get("content-type", "").startswith("text/html"):
        raise RuntimeError(
            f"Got a webpage instead of a file from this Drive link — make sure it's shared as "
            f"'Anyone with the link' can view: {url}"
        )

    filename = f"{file_id}"
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r'filename="?([^";]+)"?', cd)
    if m:
        filename = m.group(1)
    elif "." not in filename:
        filename += ".jpg"  # fallback guess so Meta's upload can infer a type

    return content, filename


# --------------------------------------------------------------------------
# Dynamic creative — one ad with several image sizes and several text options;
# Meta automatically mixes and matches them. Needs the ad set to have
# "Dynamic creative" turned on (usually set when the ad set itself was created).
# --------------------------------------------------------------------------
def set_ad_set_dynamic_creative(ad_set_id):
    url = f"{BASE_URL}/{ad_set_id}"
    resp = requests.post(url, data={"is_dynamic_creative": "true", "access_token": access_token})
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(
            f"Couldn't turn on Dynamic Creative for this ad set: {data}. "
            f"This usually has to be enabled when the ad set is first created in Ads Manager."
        )


def create_dynamic_creative(ad_name, image_hashes, primary_texts, headlines, link_url,
                             descriptions, cta) -> str:
    url = f"{BASE_URL}/{ad_account_id}/adcreatives"

    asset_feed_spec = {
        "images": [{"hash": h, "adlabels": [{"name": f"image_{i}"}]} for i, h in enumerate(image_hashes)],
        "bodies": [{"text": t, "adlabels": [{"name": f"body_{i}"}]} for i, t in enumerate(primary_texts)],
        "titles": [{"text": t, "adlabels": [{"name": f"title_{i}"}]} for i, t in enumerate(headlines)],
        "link_urls": [{"website_url": link_url, "adlabels": [{"name": "link_1"}]}],
        "call_to_action_types": [cta],
        "ad_formats": ["SINGLE_IMAGE"],
    }
    if descriptions:
        asset_feed_spec["descriptions"] = [{"text": t, "adlabels": [{"name": f"desc_{i}"}]} for i, t in enumerate(descriptions)]

    payload = {
        "name": f"{ad_name}_creative",
        "object_story_spec": {"page_id": page_id},
        "asset_feed_spec": asset_feed_spec,
        "access_token": access_token,
    }
    resp = requests.post(url, json=payload)
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(f"Dynamic creative creation failed: {data}")
    return data["id"]


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
st.title("Meta Bulk Ad Upload")

tab1, tab2, tab3, tab4 = st.tabs(["Single ad", "Carousel ad", "Collection ad", "Bulk from sheet"])

# ---- Tab 1: single ad, simple form -----------------------------------
with tab1:
    st.subheader("Create one ad")
    with st.form("single_ad_form"):
        col1, col2 = st.columns(2)
        with col1:
            ad_set_id = st.text_input("Ad Set ID")
            ad_name = st.text_input("Ad Name")
            creative_type = st.radio("Creative type", ["image", "video"], horizontal=True)
            uploaded_file = st.file_uploader("Upload creative", type=["jpg", "jpeg", "png", "mp4", "mov"])
        with col2:
            primary_text = st.text_area("Primary text")
            headline = st.text_input("Headline")
            description = st.text_input("Description (optional)")
            link_url = st.text_input("Link URL")
            cta = st.selectbox("Call to action", [
                "LEARN_MORE", "SHOP_NOW", "SIGN_UP", "BOOK_TRAVEL", "DOWNLOAD", "GET_QUOTE",
            ])
            status = st.radio("Status", ["PAUSED", "ACTIVE"], horizontal=True,
                               help="Leave as PAUSED to review in Ads Manager before it spends money.")

        submitted = st.form_submit_button("Create ad")

    if submitted:
        if not creds_ready():
            st.error("Fill in your Access Token, Ad Account ID, and Page ID in the sidebar first.")
        elif not all([ad_set_id, ad_name, uploaded_file, primary_text, headline, link_url]):
            st.error("Please fill in all required fields and upload a creative file.")
        else:
            try:
                with st.spinner("Uploading creative and creating ad..."):
                    file_bytes = uploaded_file.getvalue()
                    if creative_type == "image":
                        image_hash = upload_image(file_bytes, uploaded_file.name)
                        creative_id = create_ad_creative(ad_name, primary_text, headline, description,
                                                          link_url, cta, image_hash=image_hash)
                    else:
                        video_id = upload_video(file_bytes, uploaded_file.name)
                        creative_id = create_ad_creative(ad_name, primary_text, headline, description,
                                                          link_url, cta, video_id=video_id)
                    ad_id = create_ad(ad_name, ad_set_id, creative_id, status)
                st.success(f"Created ad '{ad_name}' — ID: {ad_id} (status: {status})")
            except Exception as e:
                st.error(f"Failed: {e}")

# ---- Tab 2: carousel ad -------------------------------------------------
with tab2:
    st.subheader("Create a carousel ad (multiple images/videos, each with its own link)")
    num_cards = st.number_input("Number of cards", min_value=2, max_value=10, value=3, step=1)

    with st.form("carousel_form"):
        col1, col2 = st.columns(2)
        with col1:
            car_ad_set_id = st.text_input("Ad Set ID", key="car_adset")
            car_ad_name = st.text_input("Ad Name", key="car_adname")
            car_primary_text = st.text_area("Primary text (shown above the carousel)", key="car_text")
        with col2:
            car_see_more_link = st.text_input("\"See more\" link (used if someone taps the ad itself)", key="car_link")
            car_cta = st.selectbox("Call to action", [
                "LEARN_MORE", "SHOP_NOW", "SIGN_UP", "BOOK_TRAVEL", "DOWNLOAD", "GET_QUOTE",
            ], key="car_cta")
            car_status = st.radio("Status", ["PAUSED", "ACTIVE"], horizontal=True, key="car_status")

        st.markdown("**Cards**")
        card_inputs = []
        for i in range(int(num_cards)):
            st.markdown(f"Card {i+1}")
            c1, c2 = st.columns(2)
            with c1:
                c_type = st.radio(f"Type (card {i+1})", ["image", "video"], horizontal=True, key=f"car_type_{i}")
                c_file = st.file_uploader(f"Upload creative (card {i+1})", type=["jpg", "jpeg", "png", "mp4", "mov"], key=f"car_file_{i}")
            with c2:
                c_headline = st.text_input(f"Headline (card {i+1})", key=f"car_headline_{i}")
                c_desc = st.text_input(f"Description (card {i+1}, optional)", key=f"car_desc_{i}")
                c_link = st.text_input(f"Link (card {i+1})", key=f"car_cardlink_{i}")
            card_inputs.append({"type": c_type, "file": c_file, "headline": c_headline,
                                 "description": c_desc, "link": c_link})

        car_submitted = st.form_submit_button("Create carousel ad")

    if car_submitted:
        if not creds_ready():
            st.error("Fill in your Access Token, Ad Account ID, and Page ID in the sidebar first.")
        elif not all([car_ad_set_id, car_ad_name, car_primary_text, car_see_more_link]):
            st.error("Fill in the Ad Set ID, Ad Name, primary text, and See more link.")
        elif any(not c["file"] or not c["headline"] or not c["link"] for c in card_inputs):
            st.error("Every card needs a creative file, headline, and link.")
        else:
            try:
                with st.spinner("Uploading creatives and creating carousel ad..."):
                    cards = []
                    for c in card_inputs:
                        file_bytes = c["file"].getvalue()
                        if c["type"] == "image":
                            image_hash = upload_image(file_bytes, c["file"].name)
                            cards.append({"link": c["link"], "headline": c["headline"],
                                          "description": c["description"], "image_hash": image_hash})
                        else:
                            video_id = upload_video(file_bytes, c["file"].name)
                            cards.append({"link": c["link"], "headline": c["headline"],
                                          "description": c["description"], "video_id": video_id})

                    creative_id = create_carousel_creative(car_ad_name, car_primary_text, car_see_more_link,
                                                            car_cta, cards)
                    ad_id = create_ad(car_ad_name, car_ad_set_id, creative_id, car_status)
                st.success(f"Created carousel ad '{car_ad_name}' — ID: {ad_id} (status: {car_status})")
            except Exception as e:
                st.error(f"Failed: {e}")

# ---- Tab 3: collection ad ------------------------------------------------
with tab3:
    st.subheader("Create a collection ad")
    st.caption(
        "Collection ads need a Product Catalog + Product Set already set up in Meta Commerce "
        "Manager — this app can't create that part for you. If creation fails, the error message "
        "below will usually say exactly what's missing or misconfigured."
    )
    with st.form("collection_form"):
        col1, col2 = st.columns(2)
        with col1:
            coll_ad_set_id = st.text_input("Ad Set ID", key="coll_adset")
            coll_ad_name = st.text_input("Ad Name", key="coll_adname")
            coll_product_set_id = st.text_input("Product Set ID (from Commerce Manager)", key="coll_productset")
            coll_cover_type = st.radio("Cover creative type", ["image", "video"], horizontal=True, key="coll_covertype")
            coll_cover_file = st.file_uploader("Upload cover image/video", type=["jpg", "jpeg", "png", "mp4", "mov"], key="coll_coverfile")
        with col2:
            coll_primary_text = st.text_area("Primary text", key="coll_text")
            coll_headline = st.text_input("Headline", key="coll_headline")
            coll_link_url = st.text_input("Link URL", key="coll_link")
            coll_cta = st.selectbox("Call to action", [
                "SHOP_NOW", "LEARN_MORE", "SIGN_UP", "GET_QUOTE",
            ], key="coll_cta")
            coll_status = st.radio("Status", ["PAUSED", "ACTIVE"], horizontal=True, key="coll_status")

        coll_submitted = st.form_submit_button("Create collection ad")

    if coll_submitted:
        if not creds_ready():
            st.error("Fill in your Access Token, Ad Account ID, and Page ID in the sidebar first.")
        elif not all([coll_ad_set_id, coll_ad_name, coll_product_set_id, coll_cover_file,
                       coll_primary_text, coll_headline, coll_link_url]):
            st.error("Please fill in all fields and upload a cover creative.")
        else:
            try:
                with st.spinner("Uploading cover creative and creating collection ad..."):
                    file_bytes = coll_cover_file.getvalue()
                    if coll_cover_type == "image":
                        cover_image_hash = upload_image(file_bytes, coll_cover_file.name)
                        creative_id = create_collection_creative(coll_ad_name, coll_primary_text, coll_headline,
                                                                  coll_link_url, coll_cta, coll_product_set_id,
                                                                  cover_image_hash=cover_image_hash)
                    else:
                        cover_video_id = upload_video(file_bytes, coll_cover_file.name)
                        creative_id = create_collection_creative(coll_ad_name, coll_primary_text, coll_headline,
                                                                  coll_link_url, coll_cta, coll_product_set_id,
                                                                  cover_video_id=cover_video_id)
                    ad_id = create_ad(coll_ad_name, coll_ad_set_id, creative_id, coll_status)
                st.success(f"Created collection ad '{coll_ad_name}' — ID: {ad_id} (status: {coll_status})")
            except Exception as e:
                st.error(f"Failed: {e}")

# ---- Tab 4: bulk from sheet (Drive links) ------------------------------
with tab4:
    st.subheader("Create many ads at once, straight from a sheet")
    st.caption(
        "One row = one ad. No file uploads needed — just paste Google Drive links directly "
        "into the sheet. Each ad can use several image sizes and several text variations at "
        "once (Meta mixes and matches them automatically) — separate multiple entries in the "
        "same cell with a pipe character `|`. Make sure every Drive file is shared as "
        "'Anyone with the link' can view, or Meta can't fetch it."
    )
    st.info(
        "This uses Meta's **Dynamic Creative** feature, which needs to be turned on for the "
        "ad set. This app tries to turn it on automatically — if the ad set was created without "
        "that option available, Meta will say so in the error and we'll know exactly what to fix."
    )

    template_df = pd.DataFrame([{
        "ad_set_id": "1234567890123",
        "ad_name": "NH_Serum_DynamicAd",
        "image_drive_links": "https://drive.google.com/file/d/AAA.../view|https://drive.google.com/file/d/BBB.../view",
        "primary_texts": "Glow up your skincare routine.|Your skin will thank you.",
        "headlines": "Shop the Glow Serum|New: Glow Serum",
        "descriptions": "Free shipping on your first order.",
        "link_url": "https://example.com/product",
        "call_to_action": "SHOP_NOW",
        "status": "PAUSED",
        "ad_set_daily_budget": "",
    }])
    st.download_button("Download sheet template", template_df.to_csv(index=False),
                        file_name="bulk_dynamic_ads_template.csv", mime="text/csv")

    csv_file = st.file_uploader("Upload your filled-in sheet (CSV)", type="csv", key="csv_uploader")
    dry_run = st.checkbox("Dry run (check the sheet without creating anything on Meta)", value=True)
    run_bulk = st.button("Process rows")

    REQUIRED_COLS = ["ad_set_id", "ad_name", "image_drive_links", "primary_texts", "headlines", "link_url"]

    if run_bulk:
        if not csv_file:
            st.error("Upload a sheet first.")
        elif not dry_run and not creds_ready():
            st.error("Fill in your Access Token, Ad Account ID, and Page ID in the sidebar first.")
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

                image_links = [x.strip() for x in row["image_drive_links"].split("|") if x.strip()]
                primary_texts = [x.strip() for x in row["primary_texts"].split("|") if x.strip()]
                headlines = [x.strip() for x in row["headlines"].split("|") if x.strip()]
                descriptions = [x.strip() for x in row.get("descriptions", "").split("|") if x.strip()]

                if dry_run:
                    results.append({"ad": row_label,
                                     "result": f"OK (dry run) — {len(image_links)} image(s), "
                                               f"{len(primary_texts)} text(s), {len(headlines)} headline(s)"})
                    continue

                try:
                    status = (row.get("status") or "PAUSED").upper()
                    cta = (row.get("call_to_action") or "LEARN_MORE").upper()

                    image_hashes = []
                    for link in image_links:
                        file_bytes, filename = download_drive_file(link)
                        image_hashes.append(upload_image(file_bytes, filename))

                    try:
                        set_ad_set_dynamic_creative(row["ad_set_id"])
                    except Exception:
                        pass  # may already be on, or the ad set may not support changing it now — proceed and let ad creation surface the real error if it matters

                    creative_id = create_dynamic_creative(
                        row["ad_name"], image_hashes, primary_texts, headlines,
                        row["link_url"], descriptions, cta,
                    )
                    ad_id = create_ad(row["ad_name"], row["ad_set_id"], creative_id, status)

                    if row.get("ad_set_daily_budget"):
                        update_ad_set_budget(row["ad_set_id"], row["ad_set_daily_budget"])

                    results.append({"ad": row_label, "result": f"Created — ID {ad_id} ({status})"})
                except Exception as e:
                    results.append({"ad": row_label, "result": f"FAILED — {e}"})

            results_df = pd.DataFrame(results)
            st.dataframe(results_df, use_container_width=True)

            fail_count = results_df["result"].str.contains("FAILED|SKIPPED", na=False).sum() if not results_df.empty else 0
            ok_count = len(results_df) - fail_count
            st.caption(f"{ok_count} succeeded, {fail_count} had issues — check the 'result' column above for exact reasons.")
