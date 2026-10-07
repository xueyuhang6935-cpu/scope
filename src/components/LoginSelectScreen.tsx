import { useState } from 'react';
import { Screen } from '../types';
import AuthLayout from './AuthLayout';
import LoginScreen from './LoginScreen';
import RegisterScreen from './RegisterScreen';

export default function LoginSelectScreen({ onNavigate }: { onNavigate: (screen: Screen) => void }) {
  const [mode, setMode] = useState<Screen.LOGIN | Screen.REGISTER>(Screen.LOGIN);
  const navigateWithin = (screen: Screen) => {
    if (screen === Screen.LOGIN_SELECT || screen === Screen.LOGIN) setMode(Screen.LOGIN);
    else if (screen === Screen.REGISTER) setMode(Screen.REGISTER);
    else onNavigate(screen);
  };

  return <AuthLayout
    onNavigate={navigateWithin}
    switchText={mode === Screen.LOGIN ? '第一次使用？' : '已有账号？'}
    switchLabel={mode === Screen.LOGIN ? '注册' : '登录'}
    switchTarget={mode === Screen.LOGIN ? Screen.REGISTER : Screen.LOGIN}
    networkChoice
  >
    <main className="scope-choice">
      <section className="scope-network-story">
        <h1>看清风险，<br /><em>让每一步决策有依据。</em></h1>
        <p>连接业务数据，识别履约风险，协同推进异常处理。</p>
        <img src="/assets/supply-network.png" alt="" />
      </section>
      <section className="scope-choice-access">
        {mode === Screen.LOGIN ? <LoginScreen embedded onNavigate={navigateWithin} /> : <RegisterScreen embedded onNavigate={navigateWithin} />}
      </section>
    </main>
  </AuthLayout>;
}
