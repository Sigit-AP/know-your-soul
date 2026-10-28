// Ground-truth JS library: shout -> formatName (same file).
export function formatName(name) {
  return name.trim();
}

export function shout(name) {
  return formatName(name).toUpperCase();
}
