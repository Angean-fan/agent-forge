export function isNonEmpty(value: string): boolean {
  return value.trim().length > 0;
}

export function canSubmitLogin(username: string, password: string): boolean {
  return isNonEmpty(username) && password.length >= 6;
}
