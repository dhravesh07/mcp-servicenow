# How to Export & Publish RefFieldFetcher

## Step 1 — Create the Update Set on Sandbox (1 minute)

1. Open sandbox: **System Definition > Scripts - Background**
2. Paste the contents of `create_update_set.js`
3. Click **Run Script**
4. You'll see output like:
   ```
   Created Update Set: <sys_id>
   Added: sys_script_include / RefFieldFetcher
   Added: sys_properties / allow_tables
   ... (9 more)
   DONE — Update Set completed with 10 records.
   ```
5. Click the link in the output to open the Update Set

## Step 2 — Export the XML (30 seconds)

1. On the Update Set form, scroll to **Related Links**
2. Click **"Export to XML"**
3. Save the file as `RefFieldFetcher_v1.0.xml`

## Step 3 — Publish to ServiceNow Share

1. Go to https://developer.servicenow.com/connect.do#!/share
2. Click **"Share a Project"**
3. Upload the XML file
4. Copy the title and description from `share-description.md`
5. Add tags: `GlideAjax, Client Script, Performance, Script Include, Security`
6. Submit

## Step 4 — Publish the Blog Post

1. Go to https://www.servicenow.com/community/developer-blog/
2. Click **"Create a blog post"**
3. Copy content from `blog-post.md`
4. Replace `[link]` at the bottom with your ServiceNow Share download URL
5. Publish

## Files in This Directory

| File | Purpose |
|------|---------|
| `create_update_set.js` | Background Script — creates a NEW Update Set with proper XML payloads |
| `populate_update_set.js` | Background Script — populates an EXISTING empty Update Set |
| `blog-post.md` | Full community blog post — copy-paste into SN Community |
| `share-description.md` | Title + description for the ServiceNow Share listing |
| `EXPORT-INSTRUCTIONS.md` | This file |
