import { Site, mount } from "../app/shell";
import { Hero, link } from "../app/bits";
import { Button } from "../conduit/ui/controls";

mount(() => <Site page="notfound" title="Not found">
  <Hero kicker="404" title={<>Nothing on this <em>frontier.</em></>} lede="That page does not exist. The subscription dashboard and the model explorers are in the sidebar."
    actions={<Button onClick={link("/")}>Back to overview</Button>} />
</Site>);
