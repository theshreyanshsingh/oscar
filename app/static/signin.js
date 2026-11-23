// The sign-in page (app/signin.py): show the password while typing it, say it is being
// checked, and copy the commands the page gives. The form works without this file too.

(() => {
  const field = document.getElementById("password");
  const toggle = document.querySelector(".password-toggle");
  const form = document.querySelector(".signin-form");

  if (field && toggle) {
    toggle.hidden = false;
    toggle.addEventListener("click", () => {
      const show = field.type === "password";
      field.type = show ? "text" : "password";
      toggle.textContent = show ? "Hide" : "Show";
      toggle.setAttribute("aria-pressed", String(show));
      toggle.setAttribute("aria-label", show ? "Hide the password" : "Show the password");
      field.focus();
    });
  }

  if (form && field) {
    const submit = form.querySelector('button[type="submit"]');
    const label = submit.querySelector(".btn-label");
    const ready = () => {
      submit.disabled = false;
      label.textContent = "Sign in";
    };
    form.addEventListener("submit", (event) => {
      if (!field.value) {
        event.preventDefault();
        let message = document.getElementById("signin-message");
        if (!message) {
          message = document.createElement("p");
          message.className = "signin-message";
          message.id = "signin-message";
          message.setAttribute("role", "alert");
          submit.before(message);
        }
        message.textContent = "Type the password.";
        field.setAttribute("aria-describedby", "signin-message");
        field.setAttribute("aria-invalid", "true");
        field.focus();
        return;
      }
      // Back to the same part of the dashboard (the browser keeps "#…" to itself).
      const next = form.elements.next;
      if (next && location.hash && !next.value.includes("#")) next.value += location.hash;
      // Saved by the browser as a password, even if it was showing.
      field.type = "password";
      submit.disabled = true;
      label.textContent = "Signing in…";
    });
    // Back to this page from the browser's history: the button works again.
    window.addEventListener("pageshow", ready);
    field.addEventListener("input", () => field.removeAttribute("aria-invalid"));
  }

  for (const button of document.querySelectorAll("[data-copy]")) {
    if (!navigator.clipboard) continue;
    button.hidden = false;
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(button.dataset.copy);
        button.textContent = "Copied";
      } catch {
        button.textContent = "Select it";
      }
      setTimeout(() => {
        button.textContent = "Copy";
      }, 1600);
    });
  }
})();
