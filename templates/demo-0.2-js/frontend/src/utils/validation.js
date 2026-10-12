export function isNonEmpty(value) {
  return value.trim().length > 0;
}

export function canSubmitLogin(username, password) {
  return isNonEmpty(username) && password.length >= 6;
}
