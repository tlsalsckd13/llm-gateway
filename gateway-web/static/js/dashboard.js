window.renderUsageChart = async function renderUsageChart(endpoint) {
  const canvas = document.getElementById("usageChart");
  if (!canvas || !window.Chart) return;

  const response = await fetch(endpoint);
  const payload = await response.json();
  const series = payload.series || [];

  new Chart(canvas, {
    type: "bar",
    data: {
      labels: series.map((item) => item.day),
      datasets: [
        {
          type: "bar",
          label: "Calls",
          data: series.map((item) => item.calls),
          backgroundColor: "rgba(22, 163, 74, 0.55)",
          borderColor: "rgb(22, 163, 74)",
          yAxisID: "y",
        },
        {
          type: "line",
          label: "Cost",
          data: series.map((item) => item.cost_usd),
          borderColor: "rgb(24, 24, 27)",
          backgroundColor: "rgb(24, 24, 27)",
          tension: 0.25,
          yAxisID: "y1",
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { position: "bottom" },
      },
      scales: {
        y: {
          beginAtZero: true,
          ticks: { precision: 0 },
        },
        y1: {
          beginAtZero: true,
          position: "right",
          grid: { drawOnChartArea: false },
        },
      },
    },
  });
};
