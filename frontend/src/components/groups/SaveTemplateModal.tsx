import React, { useEffect, useState } from 'react';
import { Alert, Button, Form, FormGroup, FormHelperText, HelperText, HelperTextItem, Modal, ModalVariant, TextArea, TextInput } from '@patternfly/react-core';
import { GroupDetail } from '../../types';
import { templateApi } from '../../services/api';
import { errorText } from '../../utils/format';

const ID_RE = /^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$/;

/** "Save as template": the group's spec becomes a user template (DATA_DIR/templates/<id>.yaml) */
export const SaveTemplateModal: React.FC<{ group: GroupDetail; isOpen: boolean; onClose: () => void; onSaved: (id: string) => void }> = ({
  group, isOpen, onClose, onSaved,
}) => {
  const [id, setId] = useState('');
  const [title, setTitle] = useState('');
  const [summary, setSummary] = useState('');
  const [tags, setTags] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!isOpen) return;
    const base = group.name.replace(/^c[0-9a-z]+-/, '') || group.name;
    setId(`my-${base}`.slice(0, 64).replace(/-+$/, ''));
    setTitle(group.spec.template?.title ? `${group.spec.template.title} (custom)` : `Lab like ${group.name}`);
    setSummary('');
    setTags('custom');
    setError(null);
  }, [isOpen, group]);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await templateApi.saveGroup({
        group_id: group.id, id, title, summary,
        tags: tags.split(/[,\s]+/).map((t) => t.trim()).filter(Boolean),
      });
      onSaved(id);
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal variant={ModalVariant.medium} title={`Save ${group.name} as a template`} isOpen={isOpen} onClose={onClose}
      actions={[
        <Button key="save" onClick={save} isLoading={busy} isDisabled={busy || !ID_RE.test(id) || !title.trim()} id="st-save">Save template</Button>,
        <Button key="cancel" variant="link" onClick={onClose}>Cancel</Button>,
      ]}>
      <p style={{ marginBottom: 16 }}>
        The spec without what this host assigned (MACs, WireGuard devices and keys, BGP ranges, cluster entries).
        Addresses of the group network become relative (<code>{'{{ip:N}}'}</code>), the name and subnet become
        parameters (case number, subnet = auto). A cluster in the group is saved with it.
        {group.spec.template?.guide ? ' The case guide is kept.' : ''}
      </p>
      {error && <Alert variant="danger" isInline title={error} style={{ marginBottom: 12 }} />}
      <Form onSubmit={(e) => { e.preventDefault(); save(); }}>
        <FormGroup label="Template id (file name)" isRequired fieldId="st-id">
          <TextInput id="st-id" value={id} onChange={(_e, v) => setId(v)} validated={id && !ID_RE.test(id) ? 'error' : 'default'} />
          <FormHelperText><HelperText><HelperTextItem>Lowercase letters, digits and '-'</HelperTextItem></HelperText></FormHelperText>
        </FormGroup>
        <FormGroup label="Title" isRequired fieldId="st-title"><TextInput id="st-title" value={title} onChange={(_e, v) => setTitle(v)} /></FormGroup>
        <FormGroup label="Summary" fieldId="st-summary"><TextArea id="st-summary" value={summary} onChange={(_e, v) => setSummary(v)} rows={2} /></FormGroup>
        <FormGroup label="Tags (comma separated)" fieldId="st-tags"><TextInput id="st-tags" value={tags} onChange={(_e, v) => setTags(v)} /></FormGroup>
      </Form>
    </Modal>
  );
};
