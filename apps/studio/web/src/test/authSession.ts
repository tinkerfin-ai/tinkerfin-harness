import type { AuthSession } from '../auth/session'

export const testAuthSession: AuthSession = {
  serverAddress: 'http://127.0.0.1:8090',
  token: 'test-session-token',
  tokenType: 'Bearer',
  expiresAt: '2099-01-01T00:00:00.000Z',
  user: {
    user_id: 7,
    username: 'tester',
    display_name: '测试用户',
    avatar_url: null,
    roles: [],
    disabled: false,
  },
}
