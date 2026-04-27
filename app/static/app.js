const taxData = JSON.parse(document.getElementById("tax-data")?.textContent || "{}");
const select  = document.getElementById("taxonomy-select");
const descBox = document.getElementById("label-description");

if (select && descBox) {
  select.addEventListener("change", () => {
    const entry = taxData[select.value];
    if (entry && entry.description) {
      descBox.textContent = entry.description;
      descBox.style.display = "block";
    } else {
      descBox.style.display = "none";
    }
  });
}
