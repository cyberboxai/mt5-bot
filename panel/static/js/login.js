// Remote access only: exchange the panel token for an HttpOnly session cookie.
const form = document.getElementById("login-form");
const msg = document.getElementById("login-msg");
form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const input = document.getElementById("token");
  const res = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: input.value }), credentials: "same-origin" });
  input.value = "";
  if (res.ok) window.location.href = "/";
  else msg.textContent = res.status === 429 ? "Demasiadas tentativas; aguarde alguns minutos." : "Token inválido.";
});
