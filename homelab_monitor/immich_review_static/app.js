/** Immich Review front end: one photo at a time, confirm or fix date/time/location. */
const state = { cards: [], index: 0, presets: [], history: [], pinned: null };
const $ = (id) => document.getElementById(id);

/** @param {string} url @param {object} [body] @returns {Promise<any>} */
async function api(url, body) {
  const init = body ? { method: 'POST', body: JSON.stringify(body) } : {};
  const response = await fetch(url, init).catch((error) => {
    throw new Error(`Could not reach the review server (${error.message}). Is it still running?`);
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

/** @returns {object[]} cards matching the selected filter */
function visibleCards() {
  const wanted = $('filter').value;
  if (wanted === 'all') return state.cards;
  return state.cards.filter((card) => statusOf(card) === wanted);
}

/** @param {object} card @returns {string} pending|applied|skipped */
function statusOf(card) {
  return card.decision ? card.decision.action === 'applied' ? 'applied' : 'skipped' : 'pending';
}

/** The card on screen: one reopened with Back, else the next in the filtered list. */
function currentCard() {
  return state.pinned || visibleCards()[state.index];
}

function describeLocation(location) {
  if (!location) return 'No location';
  const label = location.label ? `${location.label} ` : '';
  return `${label}(${location.lat.toFixed(5)}, ${location.lon.toFixed(5)})`;
}

function formatLocal(text) {
  return text.replace('T', ' ');
}

function setLocationFields(location) {
  $('lat').value = location ? location.lat : '';
  $('lon').value = location ? location.lon : '';
  updateMap();
}

function updateMap() {
  const lat = parseFloat($('lat').value);
  const lon = parseFloat($('lon').value);
  if (Number.isNaN(lat) || Number.isNaN(lon)) { $('map').removeAttribute('src'); return; }
  const box = `${lon - 0.01},${lat - 0.01},${lon + 0.01},${lat + 0.01}`;
  $('map').src = `https://www.openstreetmap.org/export/embed.html?bbox=${box}&marker=${lat},${lon}`;
}

function showNeighbor(card) {
  const near = card.suggestedLocation;
  $('neighbor').hidden = !near;
  if (!near) return;
  $('neighbor-img').src = `/img/${near.neighborId}/thumbnail`;
  const minutes = near.gapMinutes;
  $('neighbor-text').textContent = `${describeLocation(near)} — from a photo taken ${minutes} min away`;
}

function showAiGuess(card) {
  const guess = card.aiPlace;
  $('ai').hidden = !guess;
  if (!guess) return;
  $('ai-text').textContent = `AI thinks this is: ${guess.name}. ${guess.reason}`;
  $('use-ai').textContent = `Use ${guess.name}`;
}

function showDate(card) {
  $('date-current').textContent = `Current: ${formatLocal(card.takenAt)}`;
  $('date-source').textContent = `Suggestion: ${card.dateSource}`;
  $('taken').value = card.suggestedTakenAt;
}

function showDecision(card) {
  const decision = card.decision;
  $('error').textContent = decision ? `Already ${decision.action}.` : '';
}

function render() {
  const cards = visibleCards();
  state.index = Math.min(state.index, Math.max(cards.length - 1, 0));
  const card = currentCard();
  $('card').hidden = !card;
  $('empty').hidden = Boolean(card);
  const done = state.cards.filter((item) => item.decision).length;
  $('progress').textContent = `${done} of ${state.cards.length} reviewed`;
  if (card) fillCard(card);
}

function fillCard(card) {
  $('photo').src = `/img/${card.id}/preview`;
  $('name').textContent = `${card.name}${card.type === 'VIDEO' ? ' (video: still frame)' : ''}`;
  $('reasons').textContent = card.reasons.join(' · ');
  $('loc-current').textContent = `Current: ${describeLocation(card.location)}`;
  showDate(card);
  showNeighbor(card);
  showAiGuess(card);
  setLocationFields(card.location || card.aiPlace || card.suggestedLocation);
  $('places').replaceChildren();
  showDecision(card);
}

/** @returns {object|null} location typed in the fields if it differs from the current one */
function changedLocation(card) {
  const lat = parseFloat($('lat').value);
  const lon = parseFloat($('lon').value);
  if (Number.isNaN(lat) || Number.isNaN(lon)) return null;
  const old = card.location;
  return old && old.lat === lat && old.lon === lon ? null : { lat, lon };
}

/** datetime-local omits ":00" seconds; cards always carry them. */
function withSeconds(value) {
  return value.length === 16 ? `${value}:00` : value;
}

async function confirmCard() {
  const card = currentCard();
  if (!card) return;
  const typed = withSeconds($('taken').value);
  const takenAt = typed && typed !== card.takenAt ? typed : null;
  const location = changedLocation(card);
  await decide(card, { id: card.id, action: 'apply', takenAt, location });
}

async function decide(card, body) {
  try {
    card.decision = await api('/api/decision', body);
    state.history.push(card.id);
    afterDecision();
  } catch (error) {
    $('error').textContent = error.message;
  }
}

/** Move on; a card reopened with Back leaves the position in the list untouched. */
function afterDecision() {
  if (state.pinned) {
    state.pinned = null;
  } else {
    const total = visibleCards().length;
    state.index = Math.max(0, Math.min(state.index + 1 - removedByFilter(), total - 1));
  }
  render();
}

/** Back: reopen the last photo you decided (even if the filter hides it now). */
function goBack() {
  const id = state.history.pop();
  const card = state.cards.find((item) => item.id === id);
  if (!card) return;
  state.pinned = card;
  render();
}

/** When the filter is "pending", the card just decided leaves the list: don't skip one. */
function removedByFilter() {
  return $('filter').value === 'pending' ? 1 : 0;
}

async function searchPlaces() {
  const text = $('place-q').value.trim();
  if (!text) return;
  const pasted = parseCoordinates(text);
  if (pasted) {
    setLocationFields(pasted);
    $('places').replaceChildren();
    return;
  }
  const places = await api(`/api/places?q=${encodeURIComponent(text)}`);
  $('places').replaceChildren(...places.slice(0, 8).map(placeItem));
}

function placeItem(place) {
  const item = document.createElement('li');
  const region = [place.admin2name, place.admin1name].filter(Boolean).join(', ');
  item.textContent = `${place.name}${region ? `, ${region}` : ''}`;
  item.onclick = () => setLocationFields({ lat: place.latitude, lon: place.longitude });
  return item;
}

/** Buttons for the favourite places; keys 1-9 pick them when not typing. */
function renderPresets(presets) {
  state.presets = presets;
  const buttons = presets.map((preset, index) => presetButton(preset, index + 1));
  $('presets').replaceChildren(...buttons);
}

function presetButton(preset, number) {
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = `${preset.name} (${number})`;
  button.onclick = () => setLocationFields(preset);
  return button;
}

function onKey(event) {
  const typing = ['INPUT', 'SELECT'].includes(document.activeElement.tagName);
  if (event.key === 'Enter' && (event.ctrlKey || !typing)) confirmCard();
  if (typing) return;
  if (event.key === 's') $('skip').click();
  const preset = state.presets[Number(event.key) - 1];
  if (preset) setLocationFields(preset);
  if (event.key === 'ArrowLeft') $('prev').click();
}

/** @param {Function} action @param {number} ms @returns {Function} */
function debounce(action, ms) {
  let timer = 0;
  return () => { clearTimeout(timer); timer = setTimeout(action, ms); };
}

/** Pasting "lat, lon" into the latitude box fills both boxes. */
function splitPastedPair() {
  const pair = parseCoordinates($('lat').value);
  if (pair) setLocationFields(pair);
}

function wire() {
  $('confirm').onclick = confirmCard;
  $('skip').onclick = () => currentCard() && decide(currentCard(), { id: currentCard().id, action: 'skip' });
  $('prev').onclick = goBack;
  $('filter').onchange = () => { state.index = 0; state.pinned = null; render(); };
  $('place-go').onclick = searchPlaces;
  // Block body on purpose: an arrow returning `false` makes the browser cancel the keypress.
  $('place-q').onkeydown = (event) => {
    if (event.key === 'Enter') searchPlaces();
  };
  $('place-q').oninput = debounce(searchPlaces, 350);
  $('use-ai').onclick = () => setLocationFields(currentCard().aiPlace);
  $('use-neighbor').onclick = () => setLocationFields(currentCard().suggestedLocation);
  $('lat').oninput = splitPastedPair;
  $('lat').onchange = $('lon').onchange = updateMap;
  document.addEventListener('keydown', onKey);
}

async function start() {
  wire();
  renderPresets(await api('/api/presets'));
  state.cards = await api('/api/queue');
  render();
}

start().catch((error) => { $('progress').textContent = `Could not load: ${error.message}`; });
