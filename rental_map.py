"""Local rental map. Run: uv run streamlit run rental_map.py --server.address 127.0.0.1

Install: uv add 'streamlit>=1.56' supabase pandas python-dotenv
Put SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY and GOOGLE_MAPS_BROWSER_KEY
in a .env file next to this file. The Supabase key stays in Python.
Enable Maps JavaScript API for the browser key and restrict it to your website.
This app has no login: use locally or behind authentication.

Required table: rental_listings, with the columns listed in COLUMNS below.
Use GOOGLE_MAPS_MAP_ID for your own map ID; DEMO_MAP_ID is for local testing.
"""

import json
import os
from pathlib import Path
from string import Template
from urllib.parse import urlparse

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from supabase import create_client

TABLE = "rental_listings"
CATEGORIES = {"gender_preference": "Gender preference", "washroom": "Washroom"}
COLUMNS = [
    "post_uuid", "post_url", "post_text", "scraped_at", "gender_preference",
    "washroom", "available_from_date", "monthly_costs_rent_amount",
    "latitude", "longitude",
]


def as_list(value):
    """Supabase arrays/JSON lists and scalar categories share one filter path."""
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item is not None]
    if value is None or pd.isna(value):
        return []
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item) for item in parsed if item is not None]
        except ValueError:
            pass
    return [str(value)]


def safe_url(value):
    value = str(value or "").strip()
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    return value if parsed.scheme.lower() in {"https", "http"} and parsed.netloc else None


@st.cache_data(ttl=300, show_spinner=False)
def load_listings(url, key):
    client = create_client(url, key)
    rows, offset = [], 0
    # Pagination avoids Supabase's default 1,000-row response limit.
    while True:
        batch = (
            client.table(TABLE).select(",".join(COLUMNS))
            .order("post_uuid").range(offset, offset + 999).execute().data
        ) or []
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
    df = pd.DataFrame(rows, columns=COLUMNS).drop_duplicates("post_uuid", keep="last")
    for column in ("latitude", "longitude", "monthly_costs_rent_amount"):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df["post_url"] = df["post_url"].map(safe_url)
    return df


def filter_listings(df, selections, budget=None, available_by=None, query=""):
    mask = pd.Series(True, index=df.index)
    # OR within a category; AND across different categories.
    for column, selected in selections.items():
        if selected:
            wanted = set(selected)
            mask &= df[column].map(lambda value: bool(wanted.intersection(as_list(value))))
    if budget is not None:
        mask &= df["monthly_costs_rent_amount"].between(*budget)
    if available_by is not None:
        raw = df["available_from_date"].fillna("").astype(str).str.strip()
        dates = pd.to_datetime(raw, format="%Y-%m-%d", errors="coerce")
        mask &= dates.le(pd.Timestamp(available_by)) | raw.str.lower().isin(
            ["immediate", "immediately"]
        )
    if query.strip():
        mask &= df["post_text"].fillna("").str.contains(query.strip(), case=False, regex=False)
    return df.loc[mask].copy()


MAP_HTML = Template(r"""
<div id="rental-map-host" style="height:560px;width:100%;border-radius:12px;overflow:hidden">
  Loading Google Maps...
</div>
<script>
(async () => {
  const rows = $records;
  const apiKey = $api_key;
  const mapId = $map_id;
  const host = document.getElementById('rental-map-host');
  const state = window.__rentalMapState ||= {markers: [], revision: 0};
  const revision = ++state.revision;
  state.host = host;
  try {
    if (!state.loader) {
      state.loader = new Promise((resolve, reject) => {
        window.__rentalMapReady = resolve;
        window.gm_authFailure = () => {
          state.host.textContent = 'Google Maps authentication failed. Check the browser key, billing, Maps JavaScript API and website restrictions.';
        };
        const script = document.createElement('script');
        script.src = 'https://maps.googleapis.com/maps/api/js?key=' + encodeURIComponent(apiKey)
          + '&libraries=marker&v=weekly&loading=async&callback=__rentalMapReady';
        script.async = true;
        script.onerror = () => {
          state.loader = null;
          script.remove();
          reject(new Error('Could not load Google Maps. Check your connection and browser key.'));
        };
        document.head.appendChild(script);
      });
    }
    await state.loader;
    if (revision !== state.revision || !host.isConnected) return;
    if (!state.container) {
      state.container = document.createElement('div');
      state.container.style.cssText = 'height:100%;width:100%';
    }
    // Retain the map across filter changes, instead of loading a new map each time.
    host.replaceChildren(state.container);
    if (!state.map) {
      state.map = new google.maps.Map(state.container, {
        center: {lat: 17.385, lng: 78.4867}, zoom: 11, mapId,
        streetViewControl: false, mapTypeControl: false
      });
      state.info = new google.maps.InfoWindow();
    }
    state.info.close();
    state.markers.forEach(marker => { marker.map = null; });
    state.markers = [];
    const bounds = new google.maps.LatLngBounds();
    for (const row of rows) {
      const position = {lat: row.latitude, lng: row.longitude};
      const rent = row.monthly_costs_rent_amount == null
        ? 'Rent not stated' : '₹' + Number(row.monthly_costs_rent_amount).toLocaleString('en-IN') + ' / month';
      const marker = new google.maps.marker.AdvancedMarkerElement({map: state.map, position, title: rent});
      marker.addListener('click', () => {
        const content = document.createElement('div');
        content.style.cssText = 'max-width:320px;color:#17212f;font:14px/1.5 sans-serif';
        const title = document.createElement('strong');
        title.textContent = rent;
        content.appendChild(title);
        for (const text of [
          [row.gender_label, row.washroom].filter(Boolean).join(' · '),
          'Available: ' + (row.available_from_date || 'Not stated'),
          (row.post_text || '').slice(0, 600)
        ]) {
          const p = document.createElement('p');
          p.textContent = text;
          content.appendChild(p);
        }
        if (row.post_url) {
          const link = document.createElement('a');
          link.textContent = 'Open original post';
          link.href = row.post_url;
          link.target = '_blank';
          link.rel = 'noopener noreferrer';
          content.appendChild(link);
        }
        state.info.setContent(content);
        state.info.open({map: state.map, anchor: marker});
      });
      state.markers.push(marker);
      bounds.extend(position);
    }
    if (rows.length) {
      state.map.fitBounds(bounds, 50);
      google.maps.event.addListenerOnce(state.map, 'idle', () => {
        if (state.map.getZoom() > 16) state.map.setZoom(16);
      });
    }
  } catch (error) {
    host.textContent = error.message;
  }
})();
</script>
""")


def script_json(value):
    # Keep scraped text as data, including text that contains </script>.
    return json.dumps(value, ensure_ascii=True, allow_nan=False).replace("<", "\\u003c")


def render_map(df, api_key):
    points = df.copy()
    points["gender_label"] = points["gender_preference"].map(lambda value: ", ".join(as_list(value)))
    records = json.loads(points.to_json(orient="records", date_format="iso"))
    html = MAP_HTML.substitute(
        records=script_json(records), api_key=script_json(api_key),
        map_id=script_json(os.getenv("GOOGLE_MAPS_MAP_ID", "DEMO_MAP_ID")),
    )
    st.html(html, unsafe_allow_javascript=True)


def main():
    load_dotenv(Path(__file__).with_name(".env"))
    st.set_page_config(page_title="Rental map", page_icon="🏠", layout="wide")
    st.title("Find a rental")
    st.caption("Browse listings on Google Maps. Combine filters to narrow your search.")
    required = ["SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "GOOGLE_MAPS_BROWSER_KEY"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        st.info("Add these to the .env file next to rental_map.py: " + ", ".join(missing))
        st.stop()

    if st.sidebar.button("Refresh data", use_container_width=True):
        load_listings.clear()
    try:
        with st.spinner("Loading listings..."):
            df = load_listings(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    except Exception:
        st.error("Could not read rental_listings. Check the Supabase credentials and table columns, including latitude and longitude.")
        st.stop()
    if df.empty:
        st.info("No listings found. Upload the processed listings to rental_listings first.")
        st.stop()

    st.sidebar.header("Filters")
    selections = {}
    for column, label in CATEGORIES.items():
        options = sorted({item for value in df[column] for item in as_list(value)})
        selections[column] = st.sidebar.multiselect(label, options, help="Empty means all. Any selected value can match.")

    budget = None
    if st.sidebar.checkbox("Filter by monthly rent"):
        rents = df["monthly_costs_rent_amount"].dropna()
        if not rents.empty:
            low, high = float(rents.min()), float(rents.max())
            budget = st.sidebar.slider("Monthly rent (₹)", low, high, (low, high)) if low < high else (low, high)
            st.sidebar.caption("Listings with unstated rent are excluded when this filter is on.")
        else:
            budget = (0, 0)
            st.sidebar.caption("No listings have a stated rent.")

    available_by = None
    if st.sidebar.checkbox("Filter by availability"):
        available_by = st.sidebar.date_input("Available on or before")
        st.sidebar.caption("Includes immediate availability. Unknown dates are excluded.")
    query = st.sidebar.text_input("Search post text", placeholder="e.g. Kondapur or furnished")
    st.sidebar.caption("Filters combine with AND. Select both Female and Anyone to include both groups.")

    filtered = filter_listings(df, selections, budget, available_by, query)
    valid = filtered["latitude"].between(-90, 90) & filtered["longitude"].between(-180, 180)
    mapped = filtered.loc[valid]
    st.write(f"**{len(filtered):,} matching listings** · {len(mapped):,} with map coordinates")
    if filtered.empty:
        st.info("No listings match these filters. Try removing a filter.")
    else:
        if not mapped.empty:
            render_map(mapped, os.environ["GOOGLE_MAPS_BROWSER_KEY"])
        if len(mapped) < len(filtered):
            st.info(f"{len(filtered) - len(mapped):,} matching listings have missing or invalid coordinates. They remain in the table below.")
        display = filtered.drop(columns=["post_uuid"]).copy()
        display["gender_preference"] = display["gender_preference"].map(lambda value: ", ".join(as_list(value)))
        st.dataframe(display, hide_index=True, width="stretch", column_config={
            "post_url": st.column_config.LinkColumn("Original post", display_text="Open post"),
            "monthly_costs_rent_amount": st.column_config.NumberColumn("Monthly rent (₹)"),
            "gender_preference": "Gender preference", "available_from_date": "Available from",
            "post_text": "Post text", "scraped_at": "Scraped at", "washroom": "Washroom",
        })
    st.caption("Data is cached for five minutes. Use Refresh data to fetch the latest listings now.")


if __name__ == "__main__":
    main()
