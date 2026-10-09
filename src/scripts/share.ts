import { copyText } from "./clipboard";

// Share button: opens the system share sheet where supported (phones,
// Safari, Edge), otherwise copies the link to the clipboard.
function setupShare() {
  document.querySelectorAll<HTMLButtonElement>("[data-share]").forEach((button) => {
    if (button.dataset.bound === "true") return;
    button.dataset.bound = "true";
    const label = button.querySelector<HTMLElement>("[data-share-label]");
    const original = label?.textContent || "";
    button.addEventListener("click", async () => {
      const data = {
        title: button.dataset.shareTitle || document.title,
        text: button.dataset.shareText || "",
        url: button.dataset.shareUrl || location.href,
      };
      if (navigator.share && (!navigator.canShare || navigator.canShare(data))) {
        try {
          await navigator.share(data);
        } catch {
          // The user closed the share sheet.
        }
        return;
      }
      if (label) label.textContent = await copyText(data.url) ? "已复制链接" : "复制失败";
      window.setTimeout(() => {
        if (label) label.textContent = original;
      }, 2000);
    });
  });
}

document.addEventListener("astro:page-load", setupShare);
setupShare();
