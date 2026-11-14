/**
 * Read "-23.933581, -46.328830" (comma, semicolon or space separated) into coordinates.
 * @param {string} text
 * @returns {{lat: number, lon: number} | null} null when the text is not a valid pair
 * @example parseCoordinates('-23.93, -46.32') // { lat: -23.93, lon: -46.32 }
 */
function parseCoordinates(text) {
  const match = text.trim().match(/^(-?\d+(?:\.\d+)?)\s*[,;\s]\s*(-?\d+(?:\.\d+)?)$/);
  if (!match) return null;
  const lat = parseFloat(match[1]);
  const lon = parseFloat(match[2]);
  return Math.abs(lat) <= 90 && Math.abs(lon) <= 180 ? { lat, lon } : null;
}

if (typeof module !== 'undefined') module.exports = { parseCoordinates };
