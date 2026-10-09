import React, { useState } from 'react';
import { LoginForm, LoginPage as PFLoginPage } from '@patternfly/react-core';
import { ExclamationCircleIcon } from '@patternfly/react-icons';
import { AuthStatus } from '../types';
import { authApi } from '../services/api';
import { errorText } from '../utils/format';

/** Login with a Linux account of the host (PAM). Shown by AuthProvider instead of the app. */
export const LoginPage: React.FC<{ status: AuthStatus; onLogin: (s: AuthStatus) => void }> = ({ status, onLogin }) => {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.MouseEvent<HTMLButtonElement>) => {
    e.preventDefault();
    if (!username || !password || busy) return;
    setBusy(true);
    setError(null);
    try {
      onLogin(await authApi.login(username.trim(), password));
    } catch (err) {
      setError(errorText(err));
      setPassword('');
    } finally {
      setBusy(false);
    }
  };

  const groups = [...status.admin_groups, ...status.viewer_groups];
  return (
    <PFLoginPage
      loginTitle="Log in to VM Manager"
      loginSubtitle="Use your Linux account on this host"
      textContent={'VM Manager runs KVM virtual machines, lab groups and clusters on this host through libvirt.'
        + (groups.length ? ` Accounts in group ${groups.join(', ')} (or the account running VM Manager) may log in.` : '')}
    >
      <LoginForm
        showHelperText={!!error}
        helperText={error}
        helperTextIcon={<ExclamationCircleIcon />}
        usernameLabel="User name"
        usernameValue={username}
        onChangeUsername={(_e, v) => setUsername(v)}
        isValidUsername={!error}
        passwordLabel="Password"
        passwordValue={password}
        onChangePassword={(_e, v) => setPassword(v)}
        isValidPassword={!error}
        isShowPasswordEnabled
        loginButtonLabel={busy ? 'Logging in…' : 'Log in'}
        isLoginButtonDisabled={busy || !username || !password}
        onLoginButtonClick={submit}
      />
    </PFLoginPage>
  );
};
