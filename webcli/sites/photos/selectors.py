"""photos.google.com's DOM, the one place webcli photos reads the page (google-photos-cli's selectors)."""

ALBUM_LINK = 'a[href*="/album/"]'
PHOTO_LINK = 'a[href*="/photo/"]'
# A photo page's image, and the aria-labels that describe it ("Photo - Portrait - Nov 10, 2016, 11:22:34 AM").
PHOTO_IMAGE = 'img[src*="googleusercontent"], img[src*="ggpht"]'
LABELLED = "[aria-label]"
