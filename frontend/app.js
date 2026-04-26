const API_BASE_URL = window.SOLARHUNTER_API_BASE_URL || "http://localhost:8000";

const latInput = document.getElementById("latInput");
const lonInput = document.getElementById("lonInput");
const powerSlider = document.getElementById("powerSlider");
const powerInput = document.getElementById("powerInput");
const tariffSlider = document.getElementById("tariffSlider");
const tariffInput = document.getElementById("tariffInput");
const evaluateBtn = document.getElementById("evaluateBtn");
const statusText = document.getElementById("statusText");
const cfFormulaText = document.getElementById("cfFormulaText");
const resultCard = document.getElementById("resultCard");

const map = L.map("map").setView([55.75, 37.62], 4);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);

let marker = null;

function syncPair(slider, input) {
  slider.addEventListener("input", () => {
    input.value = slider.value;
  });
  input.addEventListener("input", () => {
    const value = Number(input.value);
    if (!Number.isNaN(value)) {
      slider.value = value;
    }
  });
}

syncPair(powerSlider, powerInput);
syncPair(tariffSlider, tariffInput);

function setPoint(lat, lon) {
  latInput.value = Number(lat).toFixed(6);
  lonInput.value = Number(lon).toFixed(6);
  if (marker) {
    marker.setLatLng([lat, lon]);
  } else {
    marker = L.circleMarker([lat, lon], {
      radius: 7,
      color: "#ef4444",
      fillColor: "#ef4444",
      fillOpacity: 0.9,
    }).addTo(map);
  }
}

map.on("click", (e) => setPoint(e.latlng.lat, e.latlng.lng));

latInput.addEventListener("change", () => {
  const lat = Number(latInput.value);
  const lon = Number(lonInput.value);
  if (Number.isFinite(lat) && Number.isFinite(lon)) {
    setPoint(lat, lon);
    map.setView([lat, lon], 8);
  }
});

lonInput.addEventListener("change", () => {
  const lat = Number(latInput.value);
  const lon = Number(lonInput.value);
  if (Number.isFinite(lat) && Number.isFinite(lon)) {
    setPoint(lat, lon);
    map.setView([lat, lon], 8);
  }
});

function renderResult(data) {
  const tableHtml = `
    <table>
      <tr><td>Срок окупаемости</td><td>${data.payback_years ?? "—"} лет</td></tr>
      <tr><td>Предсказанный CF</td><td>${data.cf_percent}%</td></tr>
      <tr><td>Класс CF</td><td>${data.cf_category}</td></tr>
      <tr><td>Пояснение</td><td>${data.cf_explanation}</td></tr>
      <tr><td>CAPEX</td><td>${data.capex_rub.toLocaleString("ru-RU")} руб</td></tr>
      <tr><td>OPEX/год</td><td>${data.opex_year_rub.toLocaleString("ru-RU")} руб</td></tr>
      <tr><td>REVENUE/год</td><td>${data.revenue_year_rub.toLocaleString("ru-RU")} руб</td></tr>
    </table>
  `;
  resultCard.classList.remove("hidden");
  resultCard.innerHTML = `
    <h3>Результат расчета</h3>
    ${tableHtml}
  `;
  return tableHtml;
}

evaluateBtn.addEventListener("click", async () => {
  const lat = Number(latInput.value);
  const lon = Number(lonInput.value);
  const P = Number(powerInput.value);
  const tariff = Number(tariffInput.value);

  if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
    statusText.textContent = "Сначала укажите корректные координаты.";
    cfFormulaText.innerHTML = "";
    return;
  }

  statusText.textContent = "Собираем данные и считаем прогноз. Это может занять время...";
  cfFormulaText.innerHTML = "";
  evaluateBtn.disabled = true;

  try {
    const response = await fetch(`${API_BASE_URL}/v1/evaluate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lat, lon, P, tariff }),
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const data = await response.json();
    const tableHtml = renderResult(data);
    statusText.textContent = "Готово";
    cfFormulaText.innerHTML =
      "CF = E_real / (P * T), где E_real - реальная выработка станции, P - установленная мощность станции, T = 8760 ч/год.<br>" +
      "CAPEX = 75000 * P (руб).<br>" +
      "OPEX_year = 1000 * P (руб/год).<br>" +
      "REVENUE_year = CF * 8760 * P * tariff (руб/год).<br>" +
      "Payback_years = CAPEX / (REVENUE_year - OPEX_year), если REVENUE_year > OPEX_year; иначе проект не окупается.";
    if (marker) {
      marker.bindPopup(`<div style="min-width:280px">${tableHtml}</div>`).openPopup();
    }
  } catch (error) {
    statusText.textContent = `Ошибка расчета: ${String(error)}`;
    cfFormulaText.innerHTML = "";
  } finally {
    evaluateBtn.disabled = false;
  }
});

