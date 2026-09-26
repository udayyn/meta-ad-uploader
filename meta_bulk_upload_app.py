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
import time
import mimetypes
import tempfile

import requests
import streamlit as st
import pandas as pd

GRAPH_API_VERSION = "v21.0"

st.set_page_config(page_title="Meta Bulk Ad Upload", layout="wide")

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


def update_ad_set_budget(ad_set_id, daily_budget):
    url = f"{BASE_URL}/{ad_set_id}"
    resp = requests.post(url, data={"daily_budget": daily_budget, "access_token": access_token})
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(f"Ad set budget update failed: {data}")


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
st.title("Meta Bulk Ad Upload")

tab1, tab2 = st.tabs(["Single ad", "Bulk from CSV"])

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

# ---- Tab 2: bulk from CSV ---------------------------------------------
with tab2:
    st.subheader("Create many ads at once")
    st.markdown(
        "1. Download the template below, fill in one row per ad.\n"
        "2. Upload the filled-in CSV.\n"
        "3. Upload all the creative files it references (the `file_path` column just needs to "
        "match the filename you upload here, e.g. `creative_a.jpg`).\n"
    )

    template_df = pd.DataFrame([{
        "ad_set_id": "1234567890123",
        "ad_name": "NH_Serum_Creative_A",
        "creative_type": "image",
        "file_path": "creative_a.jpg",
        "primary_text": "Glow up your skincare routine with Nat Habit.",
        "headline": "Shop the Glow Serum",
        "link_url": "https://example.com/product",
        "description": "Free shipping on your first order.",
        "call_to_action": "SHOP_NOW",
        "status": "PAUSED",
        "ad_set_daily_budget": "",
    }])
    st.download_button("Download CSV template", template_df.to_csv(index=False),
                        file_name="bulk_upload_template.csv", mime="text/csv")

    csv_file = st.file_uploader("Upload your filled-in CSV", type="csv", key="csv_uploader")
    creative_files = st.file_uploader("Upload all referenced creative files", type=["jpg", "jpeg", "png", "mp4", "mov"],
                                       accept_multiple_files=True, key="creative_uploader")
    dry_run = st.checkbox("Dry run (check the sheet without creating anything on Meta)", value=True)
    run_bulk = st.button("Process rows")

    if run_bulk:
        if not csv_file:
            st.error("Upload a CSV first.")
        elif not dry_run and not creds_ready():
            st.error("Fill in your Access Token, Ad Account ID, and Page ID in the sidebar first.")
        else:
            df = pd.read_csv(csv_file, dtype=str).fillna("")
            files_by_name = {f.name: f for f in creative_files}
            results = []

            for i, row in df.iterrows():
                row_label = row.get("ad_name", f"row {i+2}")
                missing_required = [c for c in ["ad_set_id", "ad_name", "creative_type", "file_path",
                                                 "primary_text", "headline", "link_url"] if not row.get(c)]
                file_obj = files_by_name.get(row.get("file_path"))

                if missing_required:
                    results.append({"ad": row_label, "result": f"SKIPPED — missing: {missing_required}"})
                    continue
                if not file_obj:
                    results.append({"ad": row_label, "result": f"SKIPPED — creative file not uploaded: {row.get('file_path')}"})
                    continue

                if dry_run:
                    results.append({"ad": row_label, "result": "OK (dry run)"})
                    continue

                try:
                    status = (row.get("status") or "PAUSED").upper()
                    cta = (row.get("call_to_action") or "LEARN_MORE").upper()
                    file_bytes = file_obj.getvalue()

                    if row["creative_type"].lower() == "image":
                        image_hash = upload_image(file_bytes, file_obj.name)
                        creative_id = create_ad_creative(row["ad_name"], row["primary_text"], row["headline"],
                                                          row.get("description", ""), row["link_url"], cta,
                                                          image_hash=image_hash)
                    else:
                        video_id = upload_video(file_bytes, file_obj.name)
                        creative_id = create_ad_creative(row["ad_name"], row["primary_text"], row["headline"],
                                                          row.get("description", ""), row["link_url"], cta,
                                                          video_id=video_id)

                    ad_id = create_ad(row["ad_name"], row["ad_set_id"], creative_id, status)

                    if row.get("ad_set_daily_budget"):
                        update_ad_set_budget(row["ad_set_id"], row["ad_set_daily_budget"])

                    results.append({"ad": row_label, "result": f"Created — ID {ad_id} ({status})"})
                except Exception as e:
                    results.append({"ad": row_label, "result": f"FAILED — {e}"})

            st.dataframe(pd.DataFrame(results), use_container_width=True)
