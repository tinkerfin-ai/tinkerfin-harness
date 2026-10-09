export interface AuthUser {
  user_id: number
  username: string
  avatar_url: string | null
  roles: string[]
  disabled: boolean
}

export interface LoginRequest {
  username: string
  password: string
}

export interface AuthSessionResponse {
  expires_at: string
  user: AuthUser
}

export interface LoginResponse extends AuthSessionResponse {
  access_token: string
  token_type: string
}
