import React, { useState } from 'react';
import { Button, Modal, ModalVariant } from '@patternfly/react-core';

interface ConfirmModalProps {
  title: string;
  isOpen: boolean;
  confirmLabel?: string;
  danger?: boolean;
  onConfirm: () => Promise<void> | void;
  onClose: () => void;
  children?: React.ReactNode;
}

export const ConfirmModal: React.FC<ConfirmModalProps> = ({
  title, isOpen, confirmLabel = 'Confirm', danger = true, onConfirm, onClose, children,
}) => {
  const [busy, setBusy] = useState(false);

  if (!isOpen) return null;

  const confirm = async () => {
    setBusy(true);
    try {
      await onConfirm();
    } finally {
      setBusy(false);
      onClose();
    }
  };

  return (
    <Modal
      variant={ModalVariant.small}
      title={title}
      titleIconVariant={danger ? 'warning' : undefined}
      isOpen={isOpen}
      onClose={onClose}
      actions={[
        <Button key="confirm" variant={danger ? 'danger' : 'primary'} onClick={confirm} isLoading={busy} isDisabled={busy}>
          {confirmLabel}
        </Button>,
        <Button key="cancel" variant="link" onClick={onClose}>Cancel</Button>,
      ]}
    >
      {children}
    </Modal>
  );
};
