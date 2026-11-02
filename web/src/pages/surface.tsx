// The 3D surface is a view of the explorer now; old links land on it.
location.replace(`/frontier/?${new URLSearchParams({ view: "3d", ...Object.fromEntries(new URLSearchParams(location.search)) })}`);
