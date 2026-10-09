const feedbackOpen = document.querySelector("#feedback-open");
const feedbackModal = document.querySelector("#feedback-modal");
const feedbackForm = document.querySelector("#feedback-form");
const feedbackMessage = document.querySelector("#feedback-message");
const feedbackImages = document.querySelector("#feedback-images");
const feedbackPreviews = document.querySelector("#feedback-previews");
const feedbackAdd = document.querySelector("#feedback-add");
const feedbackSubmit = document.querySelector("#feedback-submit");
const feedbackError = document.querySelector("#feedback-error");
let feedbackFiles = [];
let feedbackSending = false;
let feedbackReturnFocus = null;
let feedbackDoneTimer = null;

function feedbackClearFiles() {
  feedbackFiles.forEach(({ url }) => URL.revokeObjectURL(url));
  feedbackFiles = [];
  feedbackPreviews.replaceChildren();
  feedbackImages.value = "";
}

function feedbackRenderFiles() {
  feedbackPreviews.replaceChildren();
  feedbackFiles.forEach(({ url }, index) => {
    const preview = document.createElement("div");
    preview.className = "feedback-preview";
    const image = document.createElement("img");
    image.src = url;
    image.alt = `첨부 이미지 ${index + 1}`;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "×";
    remove.setAttribute("aria-label", `첨부 이미지 ${index + 1} 제거`);
    remove.addEventListener("click", () => {
      URL.revokeObjectURL(feedbackFiles[index].url);
      feedbackFiles.splice(index, 1);
      feedbackRenderFiles();
    });
    preview.append(image, remove);
    feedbackPreviews.append(preview);
  });
  feedbackAdd.hidden = feedbackFiles.length >= 5;
  feedbackAdd.textContent = feedbackFiles.length
    ? `사진 추가 (${feedbackFiles.length}/5)`
    : "캡처 화면 첨부 (선택, 최대 5장)";
}

function feedbackSetError(message = "") {
  feedbackError.textContent = message;
  feedbackError.hidden = !message;
}

function feedbackCloseModal() {
  if (feedbackSending) return;
  clearTimeout(feedbackDoneTimer);
  feedbackModal.hidden = true;
  feedbackClearFiles();
  feedbackReturnFocus?.focus();
}

feedbackOpen.addEventListener("click", () => {
  feedbackReturnFocus = document.activeElement;
  feedbackForm.reset();
  feedbackClearFiles();
  feedbackSetError();
  feedbackForm.classList.remove("is-done");
  document.querySelector("#feedback-count").textContent = "0/500";
  feedbackSubmit.disabled = true;
  feedbackModal.hidden = false;
  feedbackMessage.focus();
});
document.querySelector("#feedback-close").addEventListener("click", feedbackCloseModal);
feedbackModal.addEventListener("click", (event) => {
  if (event.target === feedbackModal) feedbackCloseModal();
});
document.addEventListener("keydown", (event) => {
  if (feedbackModal.hidden) return;
  if (event.key === "Escape") feedbackCloseModal();
  if (event.key !== "Tab") return;
  const controls = [...feedbackForm.querySelectorAll("button:not([hidden]):not(:disabled), textarea:not(:disabled)")]
    .filter((element) => element.getClientRects().length);
  if (!controls.length) return;
  if (event.shiftKey && document.activeElement === controls[0]) {
    event.preventDefault();
    controls.at(-1).focus();
  } else if (!event.shiftKey && document.activeElement === controls.at(-1)) {
    event.preventDefault();
    controls[0].focus();
  }
});

feedbackMessage.addEventListener("input", () => {
  const length = feedbackMessage.value.length;
  document.querySelector("#feedback-count").textContent = `${length}/500`;
  feedbackSubmit.disabled = feedbackMessage.value.trim().length < 5;
  feedbackSetError();
});
feedbackAdd.addEventListener("click", () => feedbackImages.click());
feedbackImages.addEventListener("change", () => {
  const selected = [...feedbackImages.files].slice(0, 5 - feedbackFiles.length);
  for (const file of selected) {
    if (!file.type.startsWith("image/") || file.size > 5 * 1024 * 1024) {
      feedbackSetError("이미지는 장당 5MB 이하로 첨부해 주세요.");
      continue;
    }
    feedbackFiles.push({ file, url: URL.createObjectURL(file) });
  }
  feedbackImages.value = "";
  feedbackRenderFiles();
});

feedbackForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (feedbackSending || feedbackMessage.value.trim().length < 5) return;
  feedbackSending = true;
  feedbackSubmit.disabled = true;
  feedbackSubmit.textContent = "전송 중...";
  feedbackSetError();
  const data = new FormData();
  data.append("message", feedbackMessage.value.trim());
  data.append("screenPath", location.hash || location.pathname);
  feedbackFiles.forEach(({ file }) => data.append("images", file));
  try {
    const response = await fetch("/api/feedback", { method: "POST", body: data });
    if (!response.ok) {
      const result = await response.json().catch(() => ({}));
      throw new Error(result.detail || "전송 실패, 다시 시도해 주세요.");
    }
    feedbackForm.classList.add("is-done");
    feedbackDoneTimer = setTimeout(feedbackCloseModal, 1400);
  } catch (error) {
    feedbackSetError(error.message || "전송 실패, 다시 시도해 주세요.");
  } finally {
    feedbackSending = false;
    feedbackSubmit.textContent = "제보하기";
    feedbackSubmit.disabled = feedbackMessage.value.trim().length < 5;
  }
});
