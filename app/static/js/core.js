(function () {
  "use strict";

  window.apiJson = async function apiJson(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) {
      let detail = "Request failed";
      try {
        detail = (await response.json()).detail || detail;
      } catch (error) {
        console.warn("Unable to decode API error response", error);
      }
      throw new Error(detail);
    }
    return response.status === 204 ? null : response.json();
  };

  window.escapeHtml = function escapeHtml(value) {
    if (value === null || value === undefined) return "";
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  };
})();
