// The connect command is a token wide; copying it by hand is a drag across a
// scrollbar. The only script on the page.

async function copyNode(node) {
  const text = node.textContent.trim();
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  // navigator.clipboard is absent over plain http, which is how a LAN server is
  // reached; selecting the node gives execCommand something to take instead.
  const range = document.createRange();
  range.selectNodeContents(node);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
  if (!document.execCommand("copy")) {
    throw new Error("copy refused");
  }
  selection.removeAllRanges();
}

for (const button of document.querySelectorAll("button.copy")) {
  const target = button.closest(".command-box").querySelector(".command");
  button.addEventListener("click", async () => {
    try {
      await copyNode(target);
      button.textContent = "Copied";
    } catch {
      button.textContent = "Select it and copy";
    }
    setTimeout(() => { button.textContent = "Copy"; }, 2000);
  });
}
