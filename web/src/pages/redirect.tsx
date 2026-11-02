// /frontier/ and /surface/ are the Models page now; old links keep their query, and /surface/ opens in 3D.
const query = new URLSearchParams(location.search);
if (location.pathname.startsWith("/surface") && !query.has("z")) { query.set("x", "res:cost"); query.set("y", "res:time"); query.set("z", "score"); }
location.replace(`/models/?${query}`);
