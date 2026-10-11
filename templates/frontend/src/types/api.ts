export interface UserView {
  id: number;
  username: string;
  role: string;
}

export interface LoginResponse {
  token: string;
  user: UserView;
}
