import { FinspacePrototype } from '../prototype';

export default async function CatchAllPage({ params }: { params: Promise<{ slug: string[] }> }) {
  const { slug } = await params;
  return <FinspacePrototype initialPath={slug[0]} />;
}
