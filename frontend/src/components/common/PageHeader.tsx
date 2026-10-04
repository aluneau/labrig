import React from 'react';
import { PageSection, Split, SplitItem, Text, TextContent, Title } from '@patternfly/react-core';

export const PageHeader: React.FC<{ title: string; description?: string; actions?: React.ReactNode }> = ({
  title, description, actions,
}) => (
  <PageSection variant="light">
    <Split hasGutter>
      <SplitItem isFilled>
        <Title headingLevel="h1">{title}</Title>
        {description && (
          <TextContent><Text component="p">{description}</Text></TextContent>
        )}
      </SplitItem>
      {actions && <SplitItem>{actions}</SplitItem>}
    </Split>
  </PageSection>
);
