import React, { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import {
  Alert,
  AlertActionCloseButton,
  Button,
  Card,
  CardBody,
  CardTitle,
  Checkbox,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  Flex,
  FlexItem,
  Grid,
  GridItem,
  PageSection,
  Spinner,
} from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { BareMetalHost } from '../types';
import { bareMetalApi } from '../services/api';
import { usePolling } from '../hooks/usePolling';
import { errorText } from '../utils/format';
import { PageHeader } from '../components/common/PageHeader';
import { StatusLabel } from '../components/common/StatusLabel';
import { ConfirmModal } from '../components/common/ConfirmModal';

const dash = (v: unknown) => (v === null || v === undefined || v === '' ? '—' : String(v));

export const BareMetalHostDetailPage: React.FC = () => {
  const { id } = useParams();
  const hostId = Number(id);
  const navigate = useNavigate();
  const { data: host, error: loadError, loading, reload } = usePolling<BareMetalHost>(
    () => bareMetalApi.get(hostId), 15000);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(Date.now());
  const [auto, setAuto] = useState(false);
  const [consoleError, setConsoleError] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);

  useEffect(() => {
    if (!auto) return;
    const t = setInterval(() => setTick(Date.now()), 5000);
    return () => clearInterval(t);
  }, [auto]);

  const run = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      setError(null);
    } catch (err) {
      setError(errorText(err));
    }
    reload();
  };

  if (loading && !host) return <PageSection><Spinner size="xl" /></PageSection>;
  if (!host) return <PageSection><Alert variant="danger" isInline title={loadError || 'Host not found'} /></PageSection>;

  const inv = host.inventory || {};
  const on = (inv.power_state || '').toLowerCase() === 'on';
  const busy = !!host.cluster_name;

  const refreshConsole = () => { setConsoleError(false); setTick(Date.now()); };

  const actions = (
    <Flex spaceItems={{ default: 'spaceItemsSm' }}>
      {on
        ? <FlexItem><Button variant="secondary" onClick={() => run(() => bareMetalApi.power(hostId, 'off', true))}>Power off</Button></FlexItem>
        : <FlexItem><Button variant="secondary" onClick={() => run(() => bareMetalApi.power(hostId, 'on'))}>Power on</Button></FlexItem>}
      <FlexItem><Button variant="secondary" onClick={() => run(() => bareMetalApi.power(hostId, 'cycle'))}>Power cycle</Button></FlexItem>
      <FlexItem><Button variant="secondary" onClick={() => run(() => bareMetalApi.refresh(hostId))}>Refresh inventory</Button></FlexItem>
      <FlexItem>
        <Button variant="secondary" component="a" href={host.bmc_address} target="_blank" rel="noreferrer">Open BMC web UI</Button>
      </FlexItem>
      <FlexItem>
        <Button isDisabled={busy} onClick={() => navigate('/clusters', { state: { createBmhId: hostId } })}>
          Install OpenShift (SNO)
        </Button>
      </FlexItem>
      <FlexItem>
        <Button variant="danger" isDisabled={busy} onClick={() => setConfirmReset(true)}>Reset</Button>
      </FlexItem>
    </Flex>
  );

  return (
    <>
      <PageHeader title={host.name} description={`BMC ${host.bmc_address}`} actions={actions} />
      <PageSection>
        <Link to="/bare-metal-hosts">&larr; All bare-metal hosts</Link>
        {(error || loadError) && (
          <Alert variant="danger" isInline title={error || loadError} style={{ margin: '16px 0' }}
            actionClose={error ? <AlertActionCloseButton onClose={() => setError(null)} /> : undefined} />
        )}
        <Grid hasGutter style={{ marginTop: 16 }}>
          <GridItem md={6}>
            <Card isFullHeight>
              <CardTitle>Hardware</CardTitle>
              <CardBody>
                <DescriptionList isHorizontal isCompact>
                  <DescriptionListGroup><DescriptionListTerm>Status</DescriptionListTerm>
                    <DescriptionListDescription><StatusLabel status={host.status} />
                      {host.status_message ? <div className="pf-v5-u-font-size-sm pf-v5-u-color-200">{host.status_message}</div> : null}
                    </DescriptionListDescription></DescriptionListGroup>
                  <DescriptionListGroup><DescriptionListTerm>Power</DescriptionListTerm>
                    <DescriptionListDescription><StatusLabel status={on ? 'running' : 'stopped'} /> ({dash(inv.power_state)})</DescriptionListDescription></DescriptionListGroup>
                  <DescriptionListGroup><DescriptionListTerm>Health</DescriptionListTerm>
                    <DescriptionListDescription>{dash(inv.health)}</DescriptionListDescription></DescriptionListGroup>
                  <DescriptionListGroup><DescriptionListTerm>Manufacturer / model</DescriptionListTerm>
                    <DescriptionListDescription>{dash((inv.manufacturer || '').trim())} {dash((inv.model || '').trim())}</DescriptionListDescription></DescriptionListGroup>
                  <DescriptionListGroup><DescriptionListTerm>Serial</DescriptionListTerm>
                    <DescriptionListDescription>{dash(inv.serial)}</DescriptionListDescription></DescriptionListGroup>
                  <DescriptionListGroup><DescriptionListTerm>BIOS</DescriptionListTerm>
                    <DescriptionListDescription>{dash(inv.bios_version)}</DescriptionListDescription></DescriptionListGroup>
                  <DescriptionListGroup><DescriptionListTerm>CPU</DescriptionListTerm>
                    <DescriptionListDescription>{inv.cpu_count ? `${inv.cpu_count}× ` : ''}{dash(inv.cpu_model)}</DescriptionListDescription></DescriptionListGroup>
                  <DescriptionListGroup><DescriptionListTerm>Memory</DescriptionListTerm>
                    <DescriptionListDescription>{inv.memory_gib ? `${inv.memory_gib} GiB` : '—'}</DescriptionListDescription></DescriptionListGroup>
                  <DescriptionListGroup><DescriptionListTerm>Boot override</DescriptionListTerm>
                    <DescriptionListDescription>{dash(inv.boot_override)}</DescriptionListDescription></DescriptionListGroup>
                  <DescriptionListGroup><DescriptionListTerm>Boot NIC</DescriptionListTerm>
                    <DescriptionListDescription>{dash(host.boot_mac)}</DescriptionListDescription></DescriptionListGroup>
                  <DescriptionListGroup><DescriptionListTerm>Cluster</DescriptionListTerm>
                    <DescriptionListDescription>
                      {host.cluster_name ? <Link to="/clusters">{host.cluster_name}</Link> : '—'}
                    </DescriptionListDescription></DescriptionListGroup>
                </DescriptionList>
              </CardBody>
            </Card>
          </GridItem>
          <GridItem md={6}>
            <Card isFullHeight>
              <CardTitle>
                <Flex><FlexItem flex={{ default: 'flex_1' }}>Console</FlexItem>
                  <FlexItem><Checkbox id="bmh-auto" label="Auto-refresh" isChecked={auto} onChange={(_e, v) => setAuto(v)} /></FlexItem>
                  <FlexItem><Button variant="link" isInline onClick={refreshConsole}>Refresh</Button></FlexItem>
                </Flex>
              </CardTitle>
              <CardBody>
                {consoleError ? (
                  <Alert variant="info" isInline isPlain title="Console preview not available">
                    Dell iDRAC only, or the host is off. Use <b>Open BMC web UI</b> for the full console.
                  </Alert>
                ) : (
                  <img src={`${bareMetalApi.consoleScreenshotUrl(hostId)}?t=${tick}`} alt="BMC console"
                    onError={() => setConsoleError(true)}
                    style={{ width: '100%', border: '1px solid var(--pf-v5-global--BorderColor--100)', background: '#000' }} />
                )}
                <div className="pf-v5-u-font-size-sm pf-v5-u-color-200" style={{ marginTop: 8 }}>
                  A snapshot of the host's video output (handy during an OpenShift install: POST, RHCOS boot, agent).
                </div>
              </CardBody>
            </Card>
          </GridItem>
          <GridItem span={12}>
            <Card>
              <CardTitle>Network interfaces</CardTitle>
              <CardBody>
                <Table aria-label="NICs" variant="compact" borders={false}>
                  <Thead><Tr><Th>NIC</Th><Th>MAC</Th><Th>Link</Th><Th>Speed</Th><Th>IPv4</Th><Th>Boot NIC</Th></Tr></Thead>
                  <Tbody>
                    {(inv.nics || []).map((n) => (
                      <Tr key={n.mac}>
                        <Td>{dash(n.id)}</Td><Td>{n.mac}</Td><Td>{dash(n.link)}</Td>
                        <Td>{n.speed_mbps ? `${n.speed_mbps} Mbps` : '—'}</Td>
                        <Td>{n.ipv4?.length ? n.ipv4.join(', ') : '—'}</Td>
                        <Td>
                          {host.boot_mac === n.mac ? <strong>yes</strong> : (
                            <Button variant="link" isInline isDisabled={busy}
                              onClick={() => run(() => bareMetalApi.update(hostId, { boot_mac: n.mac }))}>
                              set as boot NIC
                            </Button>
                          )}
                        </Td>
                      </Tr>
                    ))}
                    {!(inv.nics || []).length && <Tr><Td colSpan={6}>No NICs reported — refresh inventory.</Td></Tr>}
                  </Tbody>
                </Table>
              </CardBody>
            </Card>
          </GridItem>
        </Grid>
      </PageSection>

      <ConfirmModal title={`Reset ${host.name}?`} isOpen={confirmReset} confirmLabel="Wipe and power off"
        onConfirm={() => run(() => bareMetalApi.reset(hostId, true))} onClose={() => setConfirmReset(false)}>
        This <strong>erases the host's disks</strong> (Dell iDRAC: cryptographic / overwrite erase, which reboots
        into the Lifecycle Controller), ejects any virtual media and powers the machine off. The host stays
        registered. This cannot be undone.
      </ConfirmModal>
    </>
  );
};
