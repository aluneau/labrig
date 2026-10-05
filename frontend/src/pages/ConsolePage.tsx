import React, { useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import {
  Alert,
  AlertActionCloseButton,
  Breadcrumb,
  BreadcrumbItem,
  Button,
  Flex,
  FlexItem,
  PageSection,
  Spinner,
  Title,
  ToolbarGroup,
} from '@patternfly/react-core';
import { ExpandIcon, KeyboardIcon, PowerOffIcon, PlayIcon, RedoIcon, SyncAltIcon } from '@patternfly/react-icons';
import { DeviceChange, VMDetail } from '../types';
import { CdromControl } from '../components/vms/VmDevices';
import { vmApi, vncUrl } from '../services/api';
import { useLiveEvents } from '../hooks/useEvents';
import { PENDING_LABELS, useVmPower } from '../hooks/useVmPower';
import { errorText } from '../utils/format';
import { StatusLabel } from '../components/common/StatusLabel';
import { VncConsole, VncConsoleHandle, VncStatus } from '../components/console/VncConsole';

export const ConsolePage: React.FC = () => {
  const vmId = Number(useParams().id);
  const [vm, setVm] = useState<VMDetail | null>(null);
  const [notice, setNotice] = useState<DeviceChange | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [vncStatus, setVncStatus] = useState<VncStatus>('disconnected');
  const consoleRef = useRef<VncConsoleHandle>(null);
  const { pending, run, onVmEvent } = useVmPower(setError);

  useEffect(() => {
    vmApi.get(vmId).then(setVm).catch((err) => setError(errorText(err)));
  }, [vmId]);

  useLiveEvents(['vm'], (event) => {
    if (!vm || event.uuid !== vm.uuid) return;
    onVmEvent(vm.id, event);
    if (event.event === 'devices' || event.event === 'defined') vmApi.get(vmId).then(setVm).catch(() => {});
    if (event.state && event.state !== 'undefined') setVm((cur) => (cur ? { ...cur, status: event.state! } : cur));
  });

  if (!vm) {
    return error ? <PageSection><Alert variant="danger" isInline title={error} /></PageSection> : <Spinner size="xl" />;
  }

  const running = vm.status === 'running' || vm.status === 'paused';
  const busy = !!pending[vm.id];

  return (
    <>
      <PageSection variant="light" padding={{ default: 'noPadding' }} style={{ padding: '12px 24px' }}>
        <Breadcrumb>
          <BreadcrumbItem><Link to="/vms">Virtual machines</Link></BreadcrumbItem>
          <BreadcrumbItem isActive>{vm.name}</BreadcrumbItem>
        </Breadcrumb>
        <Flex alignItems={{ default: 'alignItemsCenter' }} style={{ marginTop: 8 }}>
          <FlexItem>
            <Title headingLevel="h1" size="xl">{vm.name}</Title>
          </FlexItem>
          <FlexItem>
            {busy
              ? <span className="vm-pending"><Spinner size="sm" />{PENDING_LABELS[pending[vm.id].action]}</span>
              : <StatusLabel status={vm.status} />}
          </FlexItem>
          <FlexItem align={{ default: 'alignRight' }}>
            <CdromControl
              vm={vm}
              compact
              onResult={(change) => { setNotice(change); vmApi.get(vmId).then(setVm).catch(() => {}); }}
              onError={setError}
            />
          </FlexItem>
          <FlexItem>
            <ToolbarGroup>
              {!running ? (
                <Button variant="primary" icon={<PlayIcon />} onClick={() => run(vm, 'start')} isDisabled={busy}>Start</Button>
              ) : (
                <>
                  <Button variant="secondary" icon={<KeyboardIcon />} onClick={() => consoleRef.current?.sendCtrlAltDel()}
                    isDisabled={vncStatus !== 'connected'} style={{ marginRight: 8 }}>
                    Ctrl+Alt+Del
                  </Button>
                  <Button variant="secondary" icon={<ExpandIcon />} onClick={() => consoleRef.current?.fullscreen()}
                    isDisabled={vncStatus !== 'connected'} style={{ marginRight: 8 }}>
                    Fullscreen
                  </Button>
                  <Button variant="secondary" icon={<SyncAltIcon />} onClick={() => consoleRef.current?.reconnect()} style={{ marginRight: 8 }}>
                    Reconnect
                  </Button>
                  <Button variant="secondary" icon={<RedoIcon />} onClick={() => run(vm, 'reboot')} isDisabled={busy} style={{ marginRight: 8 }}>
                    Reboot
                  </Button>
                  <Button variant="secondary" icon={<PowerOffIcon />} onClick={() => run(vm, 'stop')} isDisabled={busy} style={{ marginRight: 8 }}>
                    Shut down
                  </Button>
                  <Button variant="danger" onClick={() => run(vm, 'force_stop')}>Force off</Button>
                </>
              )}
            </ToolbarGroup>
          </FlexItem>
        </Flex>
      </PageSection>
      <PageSection padding={{ default: 'noPadding' }}>
        {error && (
          <Alert variant="danger" isInline title={error} actionClose={<AlertActionCloseButton onClose={() => setError(null)} />} />
        )}
        {notice && (
          <Alert variant={notice.pending ? 'warning' : 'info'} isInline title={notice.message}
            actionClose={<AlertActionCloseButton onClose={() => setNotice(null)} />} />
        )}
        <VncConsole
          ref={consoleRef}
          url={vncUrl(vm.id)}
          enabled={running}
          onStatus={setVncStatus}
          disabledMessage={
            <>
              <span>{vm.name} is {vm.status}.</span>
              <Button variant="primary" icon={<PlayIcon />} onClick={() => run(vm, 'start')} isDisabled={busy} isLoading={busy}>
                Start
              </Button>
            </>
          }
        />
      </PageSection>
    </>
  );
};
