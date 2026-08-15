from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import streamlit as st

st.set_page_config(page_title="DataSherlock Harness")
st.title("DataSherlock Harness")
st.caption("Local diagnosis environment")

api_base_url = os.getenv("API_BASE_URL", "http://localhost:8000")

try:
    with urllib.request.urlopen(f"{api_base_url}/health", timeout=3) as response:
        health = json.loads(response.read().decode("utf-8"))
    st.success("API is healthy")
    st.json(health)
except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
    st.error("API is unavailable")
    st.code(str(error))
